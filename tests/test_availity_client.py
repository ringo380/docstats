"""Tests for the Availity HIPAA Transactions client.

All HTTP calls are mocked — no real network requests.
"""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import httpx
import pytest

from docstats.availity_client import (
    AvailityClient,
    AvailityDisabledError,
    AvailityError,
    AvailityUnavailableError,
    COVERAGES_URL,
    PAYERS_URL,
    TOKEN_URL,
    _token_cache,
    get_availity_client,
)
from docstats.domain.eligibility import (
    EligibilityResult,
    parse_coverage_response,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clear_token_cache():
    """Reset the module-level token cache between tests."""
    _token_cache.clear()
    yield
    _token_cache.clear()


@pytest.fixture(autouse=True)
def set_env(monkeypatch):
    monkeypatch.setenv("AVAILITY_API_KEY", "test-api-key")
    monkeypatch.setenv("AVAILITY_API_SECRET", "test-api-secret")
    monkeypatch.setenv("AVAILITY_ENVIRONMENT", "sandbox")


def _mock_token_response() -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.json.return_value = {
        "access_token": "fake-bearer-token",
        "expires_in": 300,
        "token_type": "Bearer",
        "scope": "healthcare-hipaa-transactions-demo",
    }
    return resp


def _mock_coverages_response() -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.json.return_value = {
        "coverageStatus": "4",
        "memberId": "XYZ123",
        "planName": "Blue Cross PPO",
        "plans": [
            {
                "planName": "Blue Cross PPO",
                "groupNumber": "GRP001",
                "planBeginDate": "2026-01-01",
                "planEndDate": "2026-12-31",
                "referralRequired": True,
                "priorAuthorizationRequired": False,
                "benefits": [],
            }
        ],
    }
    return resp


def _mock_payers_response() -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.json.return_value = {
        "payers": [
            {"payerId": "BCBSM", "payerName": "Blue Cross Blue Shield of Michigan"},
            {"payerId": "AETNA", "payerName": "Aetna"},
        ]
    }
    return resp


# ---------------------------------------------------------------------------
# AvailityDisabledError when creds missing
# ---------------------------------------------------------------------------

def test_get_availity_client_raises_when_creds_missing(monkeypatch):
    monkeypatch.delenv("AVAILITY_API_KEY", raising=False)
    monkeypatch.delenv("AVAILITY_API_SECRET", raising=False)
    with pytest.raises(AvailityDisabledError):
        get_availity_client()


# ---------------------------------------------------------------------------
# Token acquisition
# ---------------------------------------------------------------------------

def test_token_fetched_on_first_call():
    client = AvailityClient()

    with patch.object(client._http, "request") as mock_req:
        mock_req.return_value = _mock_token_response()
        token = client._get_token()

    assert token == "fake-bearer-token"
    assert _token_cache.get("access_token") == "fake-bearer-token"


def test_token_cached_on_second_call():
    client = AvailityClient()

    with patch.object(client._http, "request") as mock_req:
        mock_req.return_value = _mock_token_response()
        client._get_token()
        client._get_token()  # should NOT call HTTP again
        assert mock_req.call_count == 1


def test_token_refreshed_when_expired():
    _token_cache["access_token"] = "old-token"
    _token_cache["expires_at"] = time.time() - 10  # already expired

    client = AvailityClient()
    with patch.object(client._http, "request") as mock_req:
        mock_req.return_value = _mock_token_response()
        token = client._get_token()

    assert token == "fake-bearer-token"


def test_token_error_raises_availity_error():
    client = AvailityClient()
    bad_resp = MagicMock(spec=httpx.Response)
    bad_resp.status_code = 401
    bad_resp.headers = {}

    with patch.object(client._http, "request", return_value=bad_resp):
        with pytest.raises(AvailityError):
            client._get_token()


# ---------------------------------------------------------------------------
# check_eligibility
# ---------------------------------------------------------------------------

SAMPLE_PAYLOAD = {
    "payerId": "BCBSM",
    "providerNpi": "1234567890",
    "memberId": "XYZ123",
    "patientBirthDate": "1980-01-15",
    "patientLastName": "Smith",
    "patientFirstName": "Jane",
    "serviceType": "30",
}


def test_check_eligibility_returns_parsed_json():
    client = AvailityClient()

    with patch.object(client._http, "request") as mock_req:
        mock_req.side_effect = [_mock_token_response(), _mock_coverages_response()]
        result = client.check_eligibility(SAMPLE_PAYLOAD)

    assert result["coverageStatus"] == "4"
    assert result["memberId"] == "XYZ123"


def test_check_eligibility_passes_scenario_id():
    client = AvailityClient()
    captured_headers: dict = {}

    def capture_request(method, url, **kwargs):
        if url == COVERAGES_URL:
            captured_headers.update(kwargs.get("headers", {}))
        return _mock_coverages_response()

    with patch.object(client._http, "request") as mock_req:
        mock_req.side_effect = [_mock_token_response(), capture_request]
        # pre-populate token so second call hits coverages
        _token_cache["access_token"] = "fake-bearer-token"
        _token_cache["expires_at"] = time.time() + 200
        mock_req.side_effect = None
        mock_req.return_value = _mock_coverages_response()
        # Inject token directly so we can verify scenario header
        _token_cache["access_token"] = "fake-bearer-token"
        _token_cache["expires_at"] = time.time() + 200

    # Separate call tracking
    client2 = AvailityClient()
    headers_seen: list[dict] = []

    original_request = client2._http.request

    def track_headers(method, url, **kwargs):
        headers_seen.append(dict(kwargs.get("headers", {})))
        return _mock_coverages_response()

    with patch.object(client2._http, "request", side_effect=track_headers):
        client2.check_eligibility(SAMPLE_PAYLOAD, scenario_id="BCBS_ACTIVE")

    assert any("X-Api-Mock-Scenario-ID" in h for h in headers_seen)


def test_check_eligibility_raises_unavailable_on_5xx():
    client = AvailityClient()
    # Pre-populate valid token
    _token_cache["access_token"] = "fake-bearer-token"
    _token_cache["expires_at"] = time.time() + 200

    bad_resp = MagicMock(spec=httpx.Response)
    bad_resp.status_code = 503
    bad_resp.headers = {}

    with patch.object(client._http, "request", return_value=bad_resp):
        with pytest.raises(AvailityUnavailableError):
            client.check_eligibility(SAMPLE_PAYLOAD)


# ---------------------------------------------------------------------------
# list_payers
# ---------------------------------------------------------------------------

def test_list_payers_returns_list():
    client = AvailityClient()
    _token_cache["access_token"] = "fake-bearer-token"
    _token_cache["expires_at"] = time.time() + 200

    with patch.object(client._http, "request", return_value=_mock_payers_response()):
        payers = client.list_payers()

    assert len(payers) == 2
    assert payers[0]["payerId"] == "BCBSM"


def test_list_payers_handles_bare_list_response():
    client = AvailityClient()
    _token_cache["access_token"] = "fake-bearer-token"
    _token_cache["expires_at"] = time.time() + 200

    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.json.return_value = [{"payerId": "AETNA"}]

    with patch.object(client._http, "request", return_value=resp):
        payers = client.list_payers()

    assert payers[0]["payerId"] == "AETNA"


# ---------------------------------------------------------------------------
# parse_coverage_response (domain layer)
# ---------------------------------------------------------------------------

def test_parse_coverage_response_active():
    data = {
        "coverageStatus": "4",
        "memberId": "MEM001",
        "plans": [
            {
                "planName": "Aetna Choice POS",
                "groupNumber": "G123",
                "planBeginDate": "2026-01-01",
                "referralRequired": True,
                "priorAuthorizationRequired": True,
                "benefits": [],
            }
        ],
    }
    result = parse_coverage_response(data)
    assert result.coverage_active is True
    assert result.coverage_status_code == "4"
    assert result.plan_name == "Aetna Choice POS"
    assert result.referral_required is True
    assert result.prior_auth_required is True


def test_parse_coverage_response_inactive():
    data = {"coverageStatus": "1", "plans": []}
    result = parse_coverage_response(data)
    assert result.coverage_active is False


def test_parse_coverage_response_missing_keys():
    """Parser must not raise on an empty/minimal response."""
    result = parse_coverage_response({})
    assert result.coverage_active is False
    assert result.plan_name is None
    assert result.referral_required is None


def test_parse_coverage_response_financial_benefits():
    data = {
        "coverageStatus": "4",
        "plans": [
            {
                "benefits": [
                    {"benefitType": "co_payment", "value": "25.0"},
                    {"benefitType": "deductible", "value": "1500.0"},
                ]
            }
        ],
    }
    result = parse_coverage_response(data)
    assert result.copay_amount == 25.0
    assert result.deductible_amount == 1500.0
