"""Tests for the Availity prior-auth (X12 278) client methods.

All HTTP calls are mocked — no real network requests.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from docstats.availity_client import (
    AvailityClient,
    AvailityError,
    AvailityUnavailableError,
    DEFAULT_AUTHORIZATIONS_URL,
    _token_cache,
)
from docstats.domain.prior_auth import (
    PA_STATUS_VALUES,
    build_idempotency_key,
    parse_authorization_response,
)


@pytest.fixture(autouse=True)
def clear_token_cache():
    _token_cache["access_token"] = "fake-bearer-token"
    _token_cache["expires_at"] = 9_999_999_999.0  # far future
    yield
    _token_cache.clear()


@pytest.fixture(autouse=True)
def set_env(monkeypatch):
    monkeypatch.setenv("AVAILITY_API_KEY", "test-api-key")
    monkeypatch.setenv("AVAILITY_API_SECRET", "test-api-secret")
    monkeypatch.setenv("AVAILITY_ENVIRONMENT", "sandbox")


def _resp(status: int, body: dict) -> MagicMock:
    r = MagicMock(spec=httpx.Response)
    r.status_code = status
    r.json.return_value = body
    r.headers = {}
    return r


SAMPLE_PAYLOAD = {
    "payerId": "BCBSM",
    "providerNpi": "1234567890",
    "requestingProviderNpi": "1234567890",
    "memberId": "MEM-1",
    "patientBirthDate": "1985-03-20",
    "patientLastName": "Smith",
    "patientFirstName": "Alice",
    "serviceType": "30",
    "diagnosisCodes": ["M54.5"],
    "procedureCodes": ["99213"],
    "serviceDate": "2026-06-01",
    "placeOfService": "11",
}


# ---------------------------------------------------------------------------
# submit_authorization
# ---------------------------------------------------------------------------


def test_submit_authorization_returns_parsed_json():
    client = AvailityClient()
    body = {
        "id": "AUTH-42",
        "status": "pending",
        "referenceNumber": None,
    }
    with patch.object(client._http, "request", return_value=_resp(200, body)):
        out = client.submit_authorization(SAMPLE_PAYLOAD, idempotency_key="ref-1-abc")
    assert out["id"] == "AUTH-42"


def test_submit_authorization_passes_idempotency_header():
    client = AvailityClient()
    captured: dict = {}

    def _capture(*args, **kwargs):
        captured["headers"] = kwargs.get("headers") or {}
        return _resp(200, {"id": "X", "status": "pending"})

    with patch.object(client._http, "request", side_effect=_capture):
        client.submit_authorization(SAMPLE_PAYLOAD, idempotency_key="ref-1-deadbeef")
    assert captured["headers"].get("Idempotency-Key") == "ref-1-deadbeef"


def test_submit_authorization_uses_default_url():
    client = AvailityClient()
    captured: dict = {}

    def _capture(method, url, **kwargs):
        captured["url"] = url
        return _resp(200, {"id": "X", "status": "pending"})

    with patch.object(client._http, "request", side_effect=_capture):
        client.submit_authorization(SAMPLE_PAYLOAD)
    assert captured["url"] == DEFAULT_AUTHORIZATIONS_URL


def test_submit_authorization_honors_url_override(monkeypatch):
    monkeypatch.setenv("AVAILITY_AUTH_URL", "https://example.test/v2/auths")
    client = AvailityClient()
    captured: dict = {}

    def _capture(method, url, **kwargs):
        captured["url"] = url
        return _resp(200, {"id": "X", "status": "pending"})

    with patch.object(client._http, "request", side_effect=_capture):
        client.submit_authorization(SAMPLE_PAYLOAD)
    assert captured["url"] == "https://example.test/v2/auths"


def test_submit_authorization_5xx_raises_unavailable():
    client = AvailityClient()
    bad = MagicMock(spec=httpx.Response)
    bad.status_code = 503
    bad.headers = {}
    with patch.object(client._http, "request", return_value=bad):
        with pytest.raises(AvailityUnavailableError):
            client.submit_authorization(SAMPLE_PAYLOAD)


def test_submit_authorization_malformed_json_raises_error():
    client = AvailityClient()
    bad = MagicMock(spec=httpx.Response)
    bad.status_code = 200
    bad.headers = {}
    bad.json.side_effect = ValueError("not json")
    with patch.object(client._http, "request", return_value=bad):
        with pytest.raises(AvailityError):
            client.submit_authorization(SAMPLE_PAYLOAD)


# ---------------------------------------------------------------------------
# get_authorization_status
# ---------------------------------------------------------------------------


def test_get_authorization_status_returns_parsed_json():
    client = AvailityClient()
    body = {"id": "AUTH-42", "status": "approved", "referenceNumber": "AUTH-XYZ"}
    with patch.object(client._http, "request", return_value=_resp(200, body)):
        out = client.get_authorization_status("AUTH-42")
    assert out["referenceNumber"] == "AUTH-XYZ"


def test_get_authorization_status_uses_id_in_url():
    client = AvailityClient()
    captured: dict = {}

    def _capture(method, url, **kwargs):
        captured["url"] = url
        captured["method"] = method
        return _resp(200, {"id": "Z", "status": "pending"})

    with patch.object(client._http, "request", side_effect=_capture):
        client.get_authorization_status("Z")
    assert captured["method"] == "GET"
    assert captured["url"].endswith("/Z")


# ---------------------------------------------------------------------------
# Domain helpers
# ---------------------------------------------------------------------------


def test_parse_authorization_response_maps_approved():
    out = parse_authorization_response(
        {
            "id": "AUTH-1",
            "status": "approved",
            "referenceNumber": "AUTH-XYZ",
            "decisionDate": "2026-05-05T10:30:00Z",
        }
    )
    assert out["status"] == "approved"
    assert out["status"] in PA_STATUS_VALUES
    assert out["reference_number"] == "AUTH-XYZ"
    assert out["availity_submission_id"] == "AUTH-1"
    assert out["decision_date"] is not None


def test_parse_authorization_response_maps_denied():
    out = parse_authorization_response(
        {
            "id": 99,
            "status": "rejected",
            "decisionReason": "Not medically necessary",
        }
    )
    assert out["status"] == "denied"
    assert out["decision_reason"] == "Not medically necessary"
    assert out["availity_submission_id"] == "99"


def test_parse_authorization_response_unknown_defaults_to_submitted():
    out = parse_authorization_response({"id": "X", "status": "weird-string"})
    assert out["status"] == "submitted"


def test_parse_authorization_response_handles_missing_id():
    out = parse_authorization_response({"status": "pending"})
    assert out["availity_submission_id"] is None
    assert out["status"] == "submitted"


def test_parse_authorization_response_invalid_date_returns_none():
    out = parse_authorization_response(
        {"id": "X", "status": "approved", "decisionDate": "not-a-date"}
    )
    assert out["decision_date"] is None


# ---------------------------------------------------------------------------
# Idempotency key
# ---------------------------------------------------------------------------


def test_build_idempotency_key_stable_across_order():
    a = build_idempotency_key(
        referral_id=42, procedure_codes=["99213", "73721"], service_date="2026-06-01"
    )
    b = build_idempotency_key(
        referral_id=42, procedure_codes=["73721", "99213"], service_date="2026-06-01"
    )
    assert a == b


def test_build_idempotency_key_changes_on_referral():
    a = build_idempotency_key(referral_id=42, procedure_codes=["99213"], service_date="2026-06-01")
    b = build_idempotency_key(referral_id=43, procedure_codes=["99213"], service_date="2026-06-01")
    assert a != b


def test_build_idempotency_key_changes_on_procedure_set():
    a = build_idempotency_key(referral_id=42, procedure_codes=["99213"], service_date="2026-06-01")
    b = build_idempotency_key(
        referral_id=42, procedure_codes=["99213", "73721"], service_date="2026-06-01"
    )
    assert a != b


def test_build_idempotency_key_normalizes_case_and_whitespace():
    a = build_idempotency_key(
        referral_id=1, procedure_codes=[" 99213 ", "73721"], service_date="2026-06-01"
    )
    b = build_idempotency_key(
        referral_id=1, procedure_codes=["99213", "73721"], service_date="2026-06-01"
    )
    assert a == b
