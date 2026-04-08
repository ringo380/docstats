"""Tests for provider enrichment: OIG client, cache, scoring integration."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from docstats.enrichment import EnrichmentCache, EnrichmentData, enrich_provider
from docstats.models import NPIResult, SavedProvider
from docstats.oig_client import OIGClient, _format_date
from docstats.scoring import SearchQuery, score_result
from tests.conftest import SAMPLE_NPI1_RESULT


# ── OIG date formatting ────────────────────────────────────────────────

class TestOIGFormatDate:
    def test_valid_date(self):
        assert _format_date("20200115") == "2020-01-15"

    def test_empty(self):
        assert _format_date("") is None

    def test_none(self):
        assert _format_date(None) is None

    def test_short(self):
        assert _format_date("2020") is None


# ── OIG Client ─────────────────────────────────────────────────────────

SAMPLE_LEIE_CSV = (
    "LASTNAME,FIRSTNAME,MIDNAME,BUSNAME,GENERAL,SPECIALTY,UPIN,NPI,"
    "DOB,ADDRESS,CITY,STATE,ZIP CODE,EXCLTYPE,EXCLDATE,REINDATE,WAIVERDATE,WAIVERSTATE\r\n"
    "DOE,JOHN,A,,GENERAL,INTERNAL MEDICINE,,1234567890,"
    "19600101,123 MAIN ST,NEW YORK,NY,10001,1128a,20190315,,,\r\n"
    "SMITH,JANE,B,,GENERAL,FAMILY PRACTICE,,9999999999,"
    "19700201,456 OAK AVE,CHICAGO,IL,60601,1128a,20180101,20200601,,\r\n"
    "CORP,,,BAD PHARMA INC,GENERAL,,,,"
    ",,789 CORP DR,DALLAS,TX,75201,1128b3,20210501,,,\r\n"
)


class TestOIGClient:
    def test_excluded_provider(self, tmp_path):
        cache_dir = tmp_path / "leie"
        cache_dir.mkdir()
        (cache_dir / "leie.csv").write_text(SAMPLE_LEIE_CSV)

        client = OIGClient(cache_dir=cache_dir)
        result = client.check_exclusion("1234567890")
        assert result is not None
        assert result["excluded"] is True
        assert result["exclusion_date"] == "2019-03-15"
        assert result["exclusion_type"] == "1128a"
        assert result["last_name"] == "DOE"
        client.close()

    def test_clean_provider(self, tmp_path):
        cache_dir = tmp_path / "leie"
        cache_dir.mkdir()
        (cache_dir / "leie.csv").write_text(SAMPLE_LEIE_CSV)

        client = OIGClient(cache_dir=cache_dir)
        result = client.check_exclusion("5555555555")
        assert result is None
        client.close()

    def test_reinstated_provider_not_excluded(self, tmp_path):
        """Providers with a REINDATE should not appear as excluded."""
        cache_dir = tmp_path / "leie"
        cache_dir.mkdir()
        (cache_dir / "leie.csv").write_text(SAMPLE_LEIE_CSV)

        client = OIGClient(cache_dir=cache_dir)
        result = client.check_exclusion("9999999999")
        assert result is None  # reinstated, so not in index
        client.close()

    def test_invalid_npi(self, tmp_path):
        cache_dir = tmp_path / "leie"
        cache_dir.mkdir()
        (cache_dir / "leie.csv").write_text(SAMPLE_LEIE_CSV)

        client = OIGClient(cache_dir=cache_dir)
        assert client.check_exclusion("123") is None
        assert client.check_exclusion("") is None
        client.close()

    def test_no_npi_records_skipped(self, tmp_path):
        """Records without NPI (org-only) should not be indexed."""
        cache_dir = tmp_path / "leie"
        cache_dir.mkdir()
        (cache_dir / "leie.csv").write_text(SAMPLE_LEIE_CSV)

        client = OIGClient(cache_dir=cache_dir)
        client._ensure_index()
        # Only 1 NPI in the index (DOE with NPI; SMITH reinstated; CORP has no NPI)
        assert len(client._npi_index) == 1
        client.close()


# ── Enrichment Cache ───────────────────────────────────────────────────

class TestEnrichmentCache:
    def test_set_and_get(self, tmp_path):
        cache = EnrichmentCache(tmp_path / "test.db")
        cache.set("oig", "1234567890", '{"excluded": true}', 86400)
        result = cache.get("oig", "1234567890")
        assert result == '{"excluded": true}'
        cache.close()

    def test_cache_miss(self, tmp_path):
        cache = EnrichmentCache(tmp_path / "test.db")
        result = cache.get("oig", "0000000000")
        assert result is None
        cache.close()

    def test_different_sources_independent(self, tmp_path):
        cache = EnrichmentCache(tmp_path / "test.db")
        cache.set("oig", "1234567890", '{"oig": true}', 86400)
        cache.set("medicare", "1234567890", '{"medicare": true}', 86400)
        assert cache.get("oig", "1234567890") == '{"oig": true}'
        assert cache.get("medicare", "1234567890") == '{"medicare": true}'
        cache.close()

    def test_clear_by_source(self, tmp_path):
        cache = EnrichmentCache(tmp_path / "test.db")
        cache.set("oig", "1234567890", '{"oig": true}', 86400)
        cache.set("medicare", "1234567890", '{"medicare": true}', 86400)
        cache.clear("oig")
        assert cache.get("oig", "1234567890") is None
        assert cache.get("medicare", "1234567890") == '{"medicare": true}'
        cache.close()

    def test_clear_all(self, tmp_path):
        cache = EnrichmentCache(tmp_path / "test.db")
        cache.set("oig", "1234567890", '{"oig": true}', 86400)
        cache.set("medicare", "1234567890", '{"medicare": true}', 86400)
        cache.clear()
        assert cache.get("oig", "1234567890") is None
        assert cache.get("medicare", "1234567890") is None
        cache.close()


# ── EnrichmentData model ──────────────────────────────────────────────

class TestEnrichmentData:
    def test_default_values(self):
        data = EnrichmentData(npi="1234567890")
        assert data.oig_excluded is None
        assert data.medicare_enrolled is None
        assert data.total_payments is None
        assert data.sources_checked == []

    def test_serialization(self):
        data = EnrichmentData(
            npi="1234567890",
            oig_excluded=True,
            oig_exclusion_date="2019-03-15",
            oig_exclusion_type="1128a",
        )
        j = data.model_dump_json()
        rehydrated = EnrichmentData.model_validate_json(j)
        assert rehydrated.oig_excluded is True
        assert rehydrated.oig_exclusion_date == "2019-03-15"


# ── Scoring with enrichment ───────────────────────────────────────────

class TestScoringWithEnrichment:
    def test_oig_exclusion_penalty(self):
        result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
        query = SearchQuery(last_name="SMITH")
        enrichment = EnrichmentData(npi="1234567890", oig_excluded=True)

        score_with = score_result(result, query, enrichment=enrichment)
        score_without = score_result(result, query)

        assert score_with < score_without
        assert score_without - score_with == 100

    def test_clean_provider_no_penalty(self):
        result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
        query = SearchQuery(last_name="SMITH")
        enrichment = EnrichmentData(npi="1234567890", oig_excluded=False)

        score_with = score_result(result, query, enrichment=enrichment)
        score_without = score_result(result, query)

        assert score_with == score_without

    def test_medicare_enrolled_boost(self):
        result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
        query = SearchQuery(last_name="SMITH")
        enrichment = EnrichmentData(npi="1234567890", medicare_enrolled=True)

        score_with = score_result(result, query, enrichment=enrichment)
        score_without = score_result(result, query)

        assert score_with == score_without + 10

    def test_none_enrichment_no_change(self):
        result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
        query = SearchQuery(last_name="SMITH")

        score_none = score_result(result, query, enrichment=None)
        score_default = score_result(result, query)

        assert score_none == score_default


# ── SavedProvider enrichment_json ─────────────────────────────────────

class TestSavedProviderEnrichment:
    def test_enrichment_json_field(self):
        result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
        provider = SavedProvider.from_npi_result(result)
        assert provider.enrichment_json is None

        enrichment = EnrichmentData(npi="1234567890", oig_excluded=False)
        provider.enrichment_json = enrichment.model_dump_json()
        assert provider.enrichment_json is not None

    def test_export_fields_with_enrichment(self):
        result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
        provider = SavedProvider.from_npi_result(result)

        enrichment = EnrichmentData(npi="1234567890", oig_excluded=True, total_payments=5432.10)
        provider.enrichment_json = enrichment.model_dump_json()

        fields = provider.export_fields()
        assert fields["OIG Excluded"] == "Yes"
        assert fields["Industry Payments ($)"] == "5432.10"

    def test_export_fields_without_enrichment(self):
        result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
        provider = SavedProvider.from_npi_result(result)

        fields = provider.export_fields()
        assert "OIG Excluded" not in fields


# ── Enrichment orchestrator ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_enrich_provider_with_oig(tmp_path):
    """Test the orchestrator calls OIG and returns enrichment data."""
    cache = EnrichmentCache(tmp_path / "test.db")

    with patch("docstats.enrichment._fetch_oig") as mock_fetch:
        mock_fetch.return_value = {"excluded": True, "exclusion_date": "2020-01-01", "exclusion_type": "1128a"}

        data = await enrich_provider("1234567890", cache)

    assert data.oig_excluded is True
    assert data.oig_exclusion_date == "2020-01-01"
    assert "oig" in data.sources_checked
    cache.close()


@pytest.mark.asyncio
async def test_enrich_provider_clean(tmp_path):
    """Test orchestrator with clean provider (not excluded)."""
    cache = EnrichmentCache(tmp_path / "test.db")

    with patch("docstats.enrichment._fetch_oig") as mock_fetch:
        mock_fetch.return_value = None

        data = await enrich_provider("5555555555", cache)

    assert data.oig_excluded is False
    assert "oig" in data.sources_checked
    cache.close()


@pytest.mark.asyncio
async def test_enrich_provider_source_failure(tmp_path):
    """Test orchestrator handles source failures gracefully."""
    cache = EnrichmentCache(tmp_path / "test.db")

    with patch("docstats.enrichment._fetch_oig") as mock_fetch:
        mock_fetch.side_effect = Exception("network error")

        data = await enrich_provider("1234567890", cache)

    assert data.oig_excluded is None  # unchecked
    assert "oig" in data.sources_failed
    cache.close()


@pytest.mark.asyncio
async def test_enrich_provider_with_medicare(tmp_path):
    """Test orchestrator integrates Medicare data."""
    cache = EnrichmentCache(tmp_path / "test.db")

    mock_medicare = {
        "enrolled": True,
        "primary_specialty": "HOSPITALIST",
        "credential": "MD",
        "medical_school": "OTHER",
        "graduation_year": "1994",
        "accepts_assignment": True,
        "telehealth": False,
        "group_affiliations": [{"name": "TEST GROUP", "pac_id": "123", "num_members": "50"}],
        "hospital_affiliations": [{"ccn": "090012", "type": "Hospital"}],
    }

    with patch("docstats.enrichment._fetch_oig") as mock_oig, \
         patch("docstats.enrichment._fetch_medicare") as mock_cms:
        mock_oig.return_value = None
        mock_cms.return_value = mock_medicare

        data = await enrich_provider("1003000126", cache)

    assert data.medicare_enrolled is True
    assert data.medicare_primary_specialty == "HOSPITALIST"
    assert data.medicare_medical_school == "OTHER"
    assert data.medicare_accepts_assignment is True
    assert len(data.group_affiliations) == 1
    assert len(data.hospital_affiliations) == 1
    assert "medicare" in data.sources_checked
    assert "oig" in data.sources_checked
    cache.close()


@pytest.mark.asyncio
async def test_enrich_provider_medicare_not_found(tmp_path):
    """Test orchestrator when provider not in Medicare data."""
    cache = EnrichmentCache(tmp_path / "test.db")

    with patch("docstats.enrichment._fetch_oig") as mock_oig, \
         patch("docstats.enrichment._fetch_medicare") as mock_cms:
        mock_oig.return_value = None
        mock_cms.return_value = None

        data = await enrich_provider("5555555555", cache)

    assert data.medicare_enrolled is False
    assert "medicare" in data.sources_checked
    cache.close()
