"""Tests for CMS Provider Data client."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from docstats.cms_client import CMSClient, CMSError


# Sample API responses matching real CMS data structure

SAMPLE_CLINICIAN_ROW = {
    "npi": "1003000126",
    "ind_pac_id": "7517003643",
    "ind_enrl_id": "I20130530000085",
    "provider_last_name": "ENKESHAFI",
    "provider_first_name": "ARDALAN",
    "provider_middle_name": "",
    "suff": "",
    "gndr": "M",
    "cred": "MD",
    "med_sch": "OTHER",
    "grd_yr": "1994",
    "pri_spec": "HOSPITALIST",
    "sec_spec_1": "INTERNAL MEDICINE",
    "sec_spec_2": "",
    "sec_spec_3": "",
    "sec_spec_4": "",
    "sec_spec_all": "INTERNAL MEDICINE",
    "telehlth": "",
    "facility_name": "MEDICAL FACULTY ASSOCIATES, INC",
    "org_pac_id": "4082528898",
    "num_org_mem": "599",
    "adr_ln_1": "1200 PECAN ST SE",
    "adr_ln_2": "",
    "ln_2_sprs": "",
    "citytown": "WASHINGTON",
    "state": "DC",
    "zip_code": "20032",
    "telephone_number": "7714446200",
    "ind_assgn": "Y",
    "grp_assgn": "Y",
    "adrs_id": "DC200320000WA1200XSEXX400",
}

SAMPLE_CLINICIAN_ROW_2 = {
    **SAMPLE_CLINICIAN_ROW,
    "facility_name": "GW MEDICAL FACULTY ASSOCIATES",
    "org_pac_id": "9876543210",
    "num_org_mem": "200",
    "adr_ln_1": "2150 PENNSYLVANIA AVE NW",
}

SAMPLE_FACILITY_ROW = {
    "npi": "1003000126",
    "ind_pac_id": "7517003643",
    "provider_last_name": "ENKESHAFI",
    "provider_first_name": "ARDALAN",
    "provider_middle_name": "",
    "suff": "",
    "facility_type": "Hospital",
    "facility_affiliations_certification_number": "090012",
    "facility_type_certification_number": "",
}


class TestCMSClientLookupClinician:
    def test_single_enrollment(self):
        client = CMSClient()
        with patch.object(client, "_query", return_value=[SAMPLE_CLINICIAN_ROW]):
            result = client.lookup_clinician("1003000126")

        assert result is not None
        assert result["enrolled"] is True
        assert result["primary_specialty"] == "HOSPITALIST"
        assert result["credential"] == "MD"
        assert result["medical_school"] == "OTHER"
        assert result["graduation_year"] == "1994"
        assert result["accepts_assignment"] is True
        assert result["telehealth"] is False
        assert len(result["group_affiliations"]) == 1
        assert result["group_affiliations"][0]["name"] == "MEDICAL FACULTY ASSOCIATES, INC"
        assert result["secondary_specialties"] == ["INTERNAL MEDICINE"]
        client.close()

    def test_multiple_group_affiliations(self):
        client = CMSClient()
        with patch.object(
            client, "_query", return_value=[SAMPLE_CLINICIAN_ROW, SAMPLE_CLINICIAN_ROW_2]
        ):
            result = client.lookup_clinician("1003000126")

        assert result is not None
        assert len(result["group_affiliations"]) == 2
        names = [g["name"] for g in result["group_affiliations"]]
        assert "MEDICAL FACULTY ASSOCIATES, INC" in names
        assert "GW MEDICAL FACULTY ASSOCIATES" in names
        client.close()

    def test_deduplicate_groups(self):
        """Same facility_name in multiple rows should only appear once."""
        client = CMSClient()
        with patch.object(
            client, "_query", return_value=[SAMPLE_CLINICIAN_ROW, SAMPLE_CLINICIAN_ROW]
        ):
            result = client.lookup_clinician("1003000126")

        assert result is not None
        assert len(result["group_affiliations"]) == 1
        client.close()

    def test_not_found(self):
        client = CMSClient()
        with patch.object(client, "_query", return_value=[]):
            result = client.lookup_clinician("0000000000")

        assert result is None
        client.close()


class TestCMSClientLookupFacilities:
    def test_with_affiliation(self):
        client = CMSClient()
        with patch.object(client, "_query", return_value=[SAMPLE_FACILITY_ROW]):
            result = client.lookup_facility_affiliations("1003000126")

        assert len(result) == 1
        assert result[0]["ccn"] == "090012"
        assert result[0]["type"] == "Hospital"
        client.close()

    def test_no_affiliations(self):
        client = CMSClient()
        with patch.object(client, "_query", return_value=[]):
            result = client.lookup_facility_affiliations("0000000000")

        assert result == []
        client.close()

    def test_deduplicate_facilities(self):
        """Same CCN in multiple rows should only appear once."""
        client = CMSClient()
        with patch.object(
            client, "_query", return_value=[SAMPLE_FACILITY_ROW, SAMPLE_FACILITY_ROW]
        ):
            result = client.lookup_facility_affiliations("1003000126")

        assert len(result) == 1
        client.close()


class TestCMSClientQuery:
    def test_retries_on_server_error(self):
        """Verify retry logic fires on 500 errors."""
        from unittest.mock import MagicMock

        client = CMSClient()
        mock_resp_500 = MagicMock()
        mock_resp_500.status_code = 500

        mock_resp_200 = MagicMock()
        mock_resp_200.status_code = 200
        mock_resp_200.json.return_value = {"results": [SAMPLE_CLINICIAN_ROW]}

        with patch.object(client._http, "request", side_effect=[mock_resp_500, mock_resp_200]):
            with patch("docstats.http_retry.time.sleep"):  # skip actual sleep
                result = client._query("mj5m-pzi6", "1003000126")

        assert len(result) == 1
        client.close()

    def test_raises_after_max_retries(self):
        from unittest.mock import MagicMock

        client = CMSClient()
        mock_resp = MagicMock()
        mock_resp.status_code = 500

        with patch.object(client._http, "request", return_value=mock_resp):
            with patch("docstats.http_retry.time.sleep"):
                with pytest.raises(CMSError):
                    client._query("mj5m-pzi6", "1003000126")
        client.close()
