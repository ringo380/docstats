"""Issue #157 — read_service_request per vendor.

Each SMART vendor (epic/cerner/eclinicalworks) exposes the same signature
``read_service_request(*, access_token, service_request_id, iss_override=None)``
returning a ``ServiceRequestSnapshot``. Redox uses ``destination_path`` instead
of ``iss_override`` for its multi-tenant routing.
"""

from __future__ import annotations

import httpx
import pytest

from docstats.ehr import (
    ServiceRequestSnapshot,
    parse_service_request_payload,
)


def _mock_transport(handler):
    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# parse_service_request_payload (shared)
# ---------------------------------------------------------------------------


def test_parse_payload_maps_known_status():
    snap = parse_service_request_payload(
        {"resourceType": "ServiceRequest", "id": "SR1", "status": "active"}
    )
    assert isinstance(snap, ServiceRequestSnapshot)
    assert snap.status == "active"
    assert snap.raw_status == "active"


def test_parse_payload_unknown_status_coerced():
    snap = parse_service_request_payload({"status": "mystery-vendor-code"})
    assert snap.status == "unknown"
    assert snap.raw_status == "mystery-vendor-code"


def test_parse_payload_missing_status():
    snap = parse_service_request_payload({})
    assert snap.status == "unknown"
    assert snap.raw_status == ""
    assert snap.last_modified is None


def test_parse_payload_last_modified_iso8601():
    snap = parse_service_request_payload(
        {"status": "active", "meta": {"lastUpdated": "2026-05-14T10:00:00Z"}}
    )
    assert snap.last_modified is not None
    assert snap.last_modified.year == 2026


def test_parse_payload_garbled_last_modified():
    snap = parse_service_request_payload(
        {"status": "active", "meta": {"lastUpdated": "not-a-date"}}
    )
    assert snap.last_modified is None


# ---------------------------------------------------------------------------
# Epic
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_epic_cache():
    from docstats.ehr import epic

    epic._DISCOVERY_CACHE.clear()
    yield
    epic._DISCOVERY_CACHE.clear()


def test_epic_read_service_request_happy_path(monkeypatch):
    from docstats.ehr import epic

    monkeypatch.setenv("EPIC_CLIENT_ID", "x")
    monkeypatch.setenv("EPIC_CLIENT_SECRET", "x")
    monkeypatch.setenv("EPIC_REDIRECT_URI", "https://referme.help/cb")
    monkeypatch.setenv("EPIC_SANDBOX_BASE_URL", "https://fake-epic.test/api/FHIR/R4")
    monkeypatch.setattr(
        "docstats.ehr.epic.discover",
        lambda **_: epic.EpicEndpoints(
            authorize_endpoint="x",
            token_endpoint="x",
            fhir_base="https://fake-epic.test/api/FHIR/R4",
        ),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert str(request.url).endswith("/ServiceRequest/SR-7")
        assert request.headers["authorization"] == "Bearer TOKEN"
        return httpx.Response(
            200,
            json={"resourceType": "ServiceRequest", "id": "SR-7", "status": "completed"},
        )

    real_client = httpx.Client
    monkeypatch.setattr(
        "docstats.ehr.epic.httpx.Client",
        lambda *a, **kw: real_client(*a, transport=_mock_transport(handler), **kw),
    )
    snap = epic.read_service_request(access_token="TOKEN", service_request_id="SR-7")
    assert snap.status == "completed"


def test_epic_read_service_request_404_raises(monkeypatch):
    from docstats.ehr import epic
    from docstats.ehr.epic import EpicError

    monkeypatch.setenv("EPIC_CLIENT_ID", "x")
    monkeypatch.setenv("EPIC_CLIENT_SECRET", "x")
    monkeypatch.setenv("EPIC_REDIRECT_URI", "https://referme.help/cb")
    monkeypatch.setenv("EPIC_SANDBOX_BASE_URL", "https://fake-epic.test/api/FHIR/R4")
    monkeypatch.setattr(
        "docstats.ehr.epic.discover",
        lambda **_: epic.EpicEndpoints(
            authorize_endpoint="x",
            token_endpoint="x",
            fhir_base="https://fake-epic.test/api/FHIR/R4",
        ),
    )
    # max_retries=0 default for non-retryable status; 404 isn't in the retryable
    # set anyway, so request_with_retry raises immediately.
    monkeypatch.setenv("DOCSTATS_HTTP_MAX_RETRIES", "0")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"resourceType": "OperationOutcome"})

    real_client = httpx.Client
    monkeypatch.setattr(
        "docstats.ehr.epic.httpx.Client",
        lambda *a, **kw: real_client(*a, transport=_mock_transport(handler), **kw),
    )
    with pytest.raises(EpicError):
        epic.read_service_request(access_token="T", service_request_id="missing")


# ---------------------------------------------------------------------------
# Cerner
# ---------------------------------------------------------------------------


def test_cerner_read_service_request(monkeypatch):
    from docstats.ehr import cerner

    monkeypatch.setenv("CERNER_CLIENT_ID", "x")
    monkeypatch.setenv("CERNER_REDIRECT_URI", "https://referme.help/cb")
    monkeypatch.setenv("CERNER_SANDBOX_BASE_URL", "https://fake-cerner.test/r4")

    monkeypatch.setattr(
        "docstats.ehr.cerner.discover",
        lambda **_: cerner.CernerEndpoints(
            authorize_endpoint="x",
            token_endpoint="x",
            fhir_base="https://fake-cerner.test/r4",
        ),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).endswith("/ServiceRequest/CERN-1")
        return httpx.Response(200, json={"status": "on-hold"})

    real_client = httpx.Client
    monkeypatch.setattr(
        "docstats.ehr.cerner.httpx.Client",
        lambda *a, **kw: real_client(*a, transport=_mock_transport(handler), **kw),
    )
    snap = cerner.read_service_request(access_token="T", service_request_id="CERN-1")
    assert snap.status == "on-hold"


# ---------------------------------------------------------------------------
# eClinicalWorks
# ---------------------------------------------------------------------------


def test_ecw_read_service_request(monkeypatch):
    from docstats.ehr import eclinicalworks as ecw

    monkeypatch.setenv("ECW_CLIENT_ID", "x")
    monkeypatch.setenv("ECW_CLIENT_SECRET", "x")
    monkeypatch.setenv("ECW_REDIRECT_URI", "https://referme.help/cb")
    monkeypatch.setenv("ECW_SANDBOX_FHIR_BASE", "https://fake-ecw.test/fhir/r4/T1")

    monkeypatch.setattr(
        "docstats.ehr.eclinicalworks.discover",
        lambda **_: ecw.ECWEndpoints(
            authorize_endpoint="x",
            token_endpoint="x",
            fhir_base="https://fake-ecw.test/fhir/r4/T1",
        ),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "active"})

    real_client = httpx.Client
    monkeypatch.setattr(
        "docstats.ehr.eclinicalworks.httpx.Client",
        lambda *a, **kw: real_client(*a, transport=_mock_transport(handler), **kw),
    )
    snap = ecw.read_service_request(access_token="T", service_request_id="ECW-9")
    assert snap.status == "active"


# ---------------------------------------------------------------------------
# Redox (uses destination_path, not iss_override)
# ---------------------------------------------------------------------------


def test_redox_read_service_request(monkeypatch):
    from docstats.ehr import redox

    monkeypatch.setenv("REDOX_FHIR_BASE", "https://fake-redox.test/fhir/R4")
    monkeypatch.setenv("REDOX_FHIR_DESTINATION", "redox-test/Development")

    def handler(request: httpx.Request) -> httpx.Response:
        # Redox path includes the destination segment.
        assert "/redox-test/Development/ServiceRequest/RDX-1" in str(request.url)
        return httpx.Response(200, json={"status": "revoked"})

    real_client = httpx.Client
    monkeypatch.setattr(
        "docstats.ehr.redox.httpx.Client",
        lambda *a, **kw: real_client(*a, transport=_mock_transport(handler), **kw),
    )
    snap = redox.read_service_request(access_token="T", service_request_id="RDX-1")
    assert snap.status == "revoked"
