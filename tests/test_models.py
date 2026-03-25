"""Tests for data models."""

from docstats.models import (
    NPIResponse,
    NPIResult,
    SavedProvider,
    BasicIndividual,
    BasicOrganization,
)
from tests.conftest import SAMPLE_API_RESPONSE, SAMPLE_NPI1_RESULT, SAMPLE_NPI2_RESULT


def test_parse_api_response():
    """Parse a full API response with both individual and org results."""
    response = NPIResponse.model_validate(SAMPLE_API_RESPONSE)
    assert response.result_count == 2
    assert len(response.results) == 2


def test_individual_result():
    """NPI-1 result has correct display name, specialty, and addresses."""
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)

    assert result.is_individual
    assert not result.is_organization
    assert result.entity_label == "Individual"
    assert result.number == "1234567890"

    # Display name should be title-cased with credential
    assert "John" in result.display_name
    assert "Smith" in result.display_name
    assert "MD" in result.display_name

    # Primary taxonomy
    assert result.primary_specialty == "Internal Medicine"
    pt = result.primary_taxonomy
    assert pt is not None
    assert pt.code == "207R00000X"
    assert pt.primary is True

    # Addresses
    assert result.location_address is not None
    assert result.location_address.city == "SAN FRANCISCO"
    assert result.location_address.formatted_postal == "94110-3518"
    assert result.location_address.formatted_phone == "(415) 555-1234"
    assert result.location_address.formatted_fax == "(415) 555-1235"

    assert result.mailing_address is not None
    assert result.mailing_address.address_purpose == "MAILING"

    # Phone/fax from location
    assert result.phone == "(415) 555-1234"
    assert result.fax == "(415) 555-1235"


def test_organization_result():
    """NPI-2 result has correct org name and type."""
    result = NPIResult.model_validate(SAMPLE_NPI2_RESULT)

    assert result.is_organization
    assert not result.is_individual
    assert result.entity_label == "Organization"
    assert "Kaiser" in result.display_name
    assert result.primary_specialty == "General Acute Care Hospital"


def test_parsed_basic_individual():
    """parsed_basic() returns BasicIndividual for NPI-1."""
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    basic = result.parsed_basic()
    assert isinstance(basic, BasicIndividual)
    assert basic.first_name == "JOHN"
    assert basic.last_name == "SMITH"
    assert basic.sex == "M"


def test_parsed_basic_organization():
    """parsed_basic() returns BasicOrganization for NPI-2."""
    result = NPIResult.model_validate(SAMPLE_NPI2_RESULT)
    basic = result.parsed_basic()
    assert isinstance(basic, BasicOrganization)
    assert basic.organization_name == "KAISER PERMANENTE MEDICAL CENTER"


def test_saved_provider_roundtrip():
    """SavedProvider can be created from NPIResult and rehydrated."""
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    saved = SavedProvider.from_npi_result(result, notes="test note")

    assert saved.npi == "1234567890"
    assert "John" in saved.display_name
    assert saved.specialty == "Internal Medicine"
    assert saved.notes == "test note"

    # Rehydrate
    rehydrated = saved.to_npi_result()
    assert rehydrated.number == result.number
    assert rehydrated.enumeration_type == result.enumeration_type
    assert len(rehydrated.addresses) == len(result.addresses)


def test_address_one_line():
    """Address.one_line produces a formatted single-line address."""
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    addr = result.location_address
    assert addr is not None
    one_line = addr.one_line
    assert "123 MAIN STREET" in one_line
    assert "SUITE 200" in one_line
    assert "SAN FRANCISCO" in one_line
    assert "94110-3518" in one_line


def test_empty_response():
    """An empty API response parses correctly."""
    response = NPIResponse.model_validate({"result_count": 0, "results": []})
    assert response.result_count == 0
    assert response.results == []


def test_result_with_no_taxonomies():
    """A result with no taxonomies returns 'Unknown' specialty."""
    data = {**SAMPLE_NPI1_RESULT, "taxonomies": []}
    result = NPIResult.model_validate(data)
    assert result.primary_specialty == "Unknown"
    assert result.primary_taxonomy is None


def test_result_with_no_addresses():
    """A result with no addresses returns None for address fields."""
    data = {**SAMPLE_NPI1_RESULT, "addresses": []}
    result = NPIResult.model_validate(data)
    assert result.location_address is None
    assert result.mailing_address is None
    assert result.phone is None
    assert result.fax is None
