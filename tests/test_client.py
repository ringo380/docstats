"""Tests for the NPPES API client."""

import json
import pytest
import httpx
from unittest.mock import MagicMock, patch

from docstats.client import NPPESClient, NPPESError
from docstats.models import NPIResponse
from tests.conftest import SAMPLE_API_RESPONSE, SAMPLE_NPI1_RESULT


@pytest.fixture
def mock_response():
    """Create a mock httpx response."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.json.return_value = SAMPLE_API_RESPONSE
    resp.raise_for_status = MagicMock()
    return resp


@pytest.fixture
def client_no_cache():
    """Client with no cache."""
    return NPPESClient(cache=None)


class TestLookup:
    def test_valid_npi(self, client_no_cache, mock_response):
        with patch.object(client_no_cache._http, "get", return_value=mock_response):
            result = client_no_cache.lookup("1234567890")
            assert result is not None
            assert result.number == "1234567890"

    def test_invalid_npi_format(self, client_no_cache):
        with pytest.raises(NPPESError, match="Invalid NPI format"):
            client_no_cache.lookup("123")

    def test_invalid_npi_letters(self, client_no_cache):
        with pytest.raises(NPPESError, match="Invalid NPI format"):
            client_no_cache.lookup("12345abcde")

    def test_npi_not_found(self, client_no_cache):
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        resp.json.return_value = {"result_count": 0, "results": []}
        resp.raise_for_status = MagicMock()

        with patch.object(client_no_cache._http, "get", return_value=resp):
            result = client_no_cache.lookup("0000000000")
            assert result is None


class TestSearch:
    def test_search_by_last_name(self, client_no_cache, mock_response):
        with patch.object(client_no_cache._http, "get", return_value=mock_response) as mock_get:
            response = client_no_cache.search(last_name="Smith", state="CA")
            assert response.result_count == 2

            # Verify correct params were sent
            call_kwargs = mock_get.call_args
            params = call_kwargs.kwargs.get("params") or call_kwargs[1].get("params")
            assert params["last_name"] == "Smith"
            assert params["state"] == "CA"
            assert params["version"] == "2.1"

    def test_search_requires_params(self, client_no_cache):
        with pytest.raises(NPPESError, match="At least one search parameter"):
            client_no_cache.search(state="CA")

    def test_search_by_org(self, client_no_cache, mock_response):
        with patch.object(client_no_cache._http, "get", return_value=mock_response):
            response = client_no_cache.search(organization_name="Kaiser")
            assert response.result_count == 2

    def test_search_with_limit(self, client_no_cache, mock_response):
        with patch.object(client_no_cache._http, "get", return_value=mock_response) as mock_get:
            client_no_cache.search(last_name="Smith", limit=50)
            params = mock_get.call_args.kwargs.get("params") or mock_get.call_args[1].get("params")
            assert params["limit"] == "50"

    def test_search_limit_capped(self, client_no_cache, mock_response):
        with patch.object(client_no_cache._http, "get", return_value=mock_response) as mock_get:
            client_no_cache.search(last_name="Smith", limit=9999)
            params = mock_get.call_args.kwargs.get("params") or mock_get.call_args[1].get("params")
            assert params["limit"] == "1200"


class TestErrorHandling:
    def test_api_error_field(self, client_no_cache):
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        resp.json.return_value = {
            "Errors": [{"description": "Invalid search criteria"}]
        }
        resp.raise_for_status = MagicMock()

        with patch.object(client_no_cache._http, "get", return_value=resp):
            with pytest.raises(NPPESError, match="Invalid search criteria"):
                client_no_cache.search(last_name="X")

    def test_network_error(self, client_no_cache):
        with patch.object(
            client_no_cache._http, "get",
            side_effect=httpx.ConnectError("Connection refused"),
        ):
            with pytest.raises(NPPESError, match="Could not reach the NPI Registry"):
                client_no_cache.search(last_name="Smith")
