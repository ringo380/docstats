"""Tests for the NPPES API client."""

import asyncio

import pytest
import httpx
from unittest.mock import MagicMock, patch

from docstats.client import NPPESClient, NPPESError
from tests.conftest import SAMPLE_API_RESPONSE


@pytest.fixture
def mock_response():
    """Create a mock httpx response."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.json.return_value = SAMPLE_API_RESPONSE
    return resp


@pytest.fixture
def client_no_cache():
    """Client with no cache."""
    return NPPESClient(cache=None)


class TestLookup:
    def test_valid_npi(self, client_no_cache, mock_response):
        with patch.object(client_no_cache._http, "request", return_value=mock_response):
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

        with patch.object(client_no_cache._http, "request", return_value=resp):
            result = client_no_cache.lookup("0000000000")
            assert result is None


class TestSearch:
    def test_search_by_last_name(self, client_no_cache, mock_response):
        with patch.object(
            client_no_cache._http, "request", return_value=mock_response
        ) as mock_request:
            response = client_no_cache.search(last_name="Smith", state="CA")
            assert response.result_count == 2

            # Verify correct params were sent
            call_kwargs = mock_request.call_args
            params = call_kwargs.kwargs.get("params") or call_kwargs[1].get("params")
            assert params["last_name"] == "Smith"
            assert params["state"] == "CA"
            assert params["version"] == "2.1"

    def test_search_requires_params(self, client_no_cache):
        with pytest.raises(NPPESError, match="At least one search parameter"):
            client_no_cache.search(state="CA")

    def test_search_by_org(self, client_no_cache, mock_response):
        with patch.object(client_no_cache._http, "request", return_value=mock_response):
            response = client_no_cache.search(organization_name="Kaiser")
            assert response.result_count == 2

    def test_search_with_limit(self, client_no_cache, mock_response):
        with patch.object(
            client_no_cache._http, "request", return_value=mock_response
        ) as mock_request:
            client_no_cache.search(last_name="Smith", limit=50)
            params = mock_request.call_args.kwargs.get("params") or mock_request.call_args[1].get(
                "params"
            )
            assert params["limit"] == "50"

    def test_search_limit_capped(self, client_no_cache, mock_response):
        with patch.object(
            client_no_cache._http, "request", return_value=mock_response
        ) as mock_request:
            client_no_cache.search(last_name="Smith", limit=9999)
            params = mock_request.call_args.kwargs.get("params") or mock_request.call_args[1].get(
                "params"
            )
            assert params["limit"] == "1200"


class TestErrorHandling:
    def test_api_error_field(self, client_no_cache):
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        resp.json.return_value = {"Errors": [{"description": "Invalid search criteria"}]}

        with patch.object(client_no_cache._http, "request", return_value=resp):
            with pytest.raises(NPPESError, match="Invalid search criteria"):
                client_no_cache.search(last_name="X")

    def test_network_error(self, client_no_cache):
        with (
            patch.object(
                client_no_cache._http,
                "request",
                side_effect=httpx.ConnectError("Connection refused"),
            ),
            patch("docstats.http_retry.time.sleep"),
        ):
            with pytest.raises(NPPESError, match="Could not reach the NPI Registry"):
                client_no_cache.search(last_name="Smith")

    def test_non_retryable_status_fails_immediately(self, client_no_cache):
        """400/404 errors should not be retried."""
        resp_400 = MagicMock(spec=httpx.Response)
        resp_400.status_code = 400
        resp_400.headers = {}

        with patch.object(client_no_cache._http, "request", return_value=resp_400) as mock_request:
            with pytest.raises(NPPESError, match="temporarily unavailable"):
                client_no_cache.search(last_name="Smith")
            assert mock_request.call_count == 1


class TestRetry:
    def test_retries_on_500(self, client_no_cache, mock_response):
        """500 errors should be retried up to max_retries times."""
        resp_500 = MagicMock(spec=httpx.Response)
        resp_500.status_code = 500
        resp_500.headers = {}

        # Fail twice, succeed on third attempt
        with (
            patch.object(
                client_no_cache._http,
                "request",
                side_effect=[resp_500, resp_500, mock_response],
            ) as mock_request,
            patch("docstats.http_retry.time.sleep") as mock_sleep,
        ):
            result = client_no_cache.search(last_name="Smith")
            assert result.result_count == 2
            assert mock_request.call_count == 3
            assert mock_sleep.call_count == 2

    def test_retries_exhausted_raises(self, client_no_cache):
        """After max_retries + 1 attempts, should raise NPPESError."""
        resp_500 = MagicMock(spec=httpx.Response)
        resp_500.status_code = 500
        resp_500.headers = {}

        with (
            patch.object(
                client_no_cache._http,
                "request",
                return_value=resp_500,
            ) as mock_request,
            patch("docstats.http_retry.time.sleep"),
        ):
            with pytest.raises(NPPESError, match="temporarily unavailable"):
                client_no_cache.search(last_name="Smith")
            assert mock_request.call_count == 4  # 1 initial + 3 retries

    def test_retries_on_timeout(self, client_no_cache, mock_response):
        """Timeout errors should be retried."""
        with (
            patch.object(
                client_no_cache._http,
                "request",
                side_effect=[httpx.ReadTimeout("timeout"), mock_response],
            ) as mock_request,
            patch("docstats.http_retry.time.sleep") as mock_sleep,
        ):
            result = client_no_cache.search(last_name="Smith")
            assert result.result_count == 2
            assert mock_request.call_count == 2
            assert mock_sleep.call_count == 1

    def test_retry_honors_retry_after_header(self, client_no_cache, mock_response):
        """429 responses with Retry-After header should use that delay."""
        resp_429 = MagicMock(spec=httpx.Response)
        resp_429.status_code = 429
        resp_429.headers = {"retry-after": "5"}

        with (
            patch.object(
                client_no_cache._http,
                "request",
                side_effect=[resp_429, mock_response],
            ),
            patch("docstats.http_retry.time.sleep") as mock_sleep,
        ):
            result = client_no_cache.search(last_name="Smith")
            assert result.result_count == 2
            mock_sleep.assert_called_once_with(5.0)

    def test_exponential_backoff_delays(self, client_no_cache):
        """Backoff delays should follow 1s, 2s, 4s pattern."""
        resp_503 = MagicMock(spec=httpx.Response)
        resp_503.status_code = 503
        resp_503.headers = {}

        with (
            patch.object(
                client_no_cache._http,
                "request",
                return_value=resp_503,
            ),
            patch("docstats.http_retry.time.sleep") as mock_sleep,
        ):
            with pytest.raises(NPPESError):
                client_no_cache.search(last_name="Smith")
            delays = [call.args[0] for call in mock_sleep.call_args_list]
            assert delays == [1.0, 2.0, 4.0]


class TestAsyncLookupMany:
    def _single_result_response(self, npi: str) -> MagicMock:
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        resp.json.return_value = {
            "result_count": 1,
            "results": [
                {
                    "enumeration_type": "NPI-1",
                    "number": npi,
                    "basic": {"first_name": "Test", "last_name": "Provider"},
                    "addresses": [],
                    "taxonomies": [],
                    "identifiers": [],
                    "endpoints": [],
                    "other_names": [],
                }
            ],
        }
        return resp

    def test_returns_results_in_input_order(self, client_no_cache):
        npis = ["1111111111", "2222222222", "3333333333"]
        responses = {npi: self._single_result_response(npi) for npi in npis}

        def _by_npi(method, url, **kwargs):
            return responses[kwargs["params"]["number"]]

        with patch.object(client_no_cache._http, "request", side_effect=_by_npi):
            results = asyncio.run(client_no_cache.async_lookup_many(npis))

        assert [r.number for r in results if r] == npis

    def test_respects_explicit_limiter(self, client_no_cache):
        npis = [f"{i:010d}" for i in range(6)]
        responses = {npi: self._single_result_response(npi) for npi in npis}
        sem = asyncio.Semaphore(2)

        in_flight = 0
        peak = 0

        def _by_npi(method, url, **kwargs):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            try:
                return responses[kwargs["params"]["number"]]
            finally:
                in_flight -= 1

        with patch.object(client_no_cache._http, "request", side_effect=_by_npi):
            results = asyncio.run(client_no_cache.async_lookup_many(npis, limiter=sem))

        assert len(results) == len(npis)
        # Executor may serialize work further, but the semaphore must never be exceeded.
        assert peak <= 2
