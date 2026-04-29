"""Cerner/Oracle Health SMART-on-FHIR client tests with mocked httpx."""

from __future__ import annotations

import httpx
import pytest

from docstats.ehr import cerner
from docstats.ehr.cerner import CernerError


@pytest.fixture(autouse=True)
def reset_discovery_cache():
    cerner._DISCOVERY_CACHE.clear()
    yield
    cerner._DISCOVERY_CACHE.clear()


@pytest.fixture
def cerner_env(monkeypatch):
    monkeypatch.setenv("CERNER_CLIENT_ID", "cerner-client")
    monkeypatch.setenv("CERNER_CLIENT_SECRET", "cerner-secret")
    monkeypatch.setenv("CERNER_REDIRECT_URI", "https://referme.help/ehr/callback/cerner")
    monkeypatch.setenv("CERNER_SANDBOX_TENANT_ID", "ec2458f2-1e24-41c8-b71b-0e701af7583d")


def _mock_transport(handler):
    return httpx.MockTransport(handler)


def _fake_endpoints():
    return cerner.CernerEndpoints(
        authorize_endpoint="https://fhir-ehr-code.cerner.com/oauth2/authorize",
        token_endpoint="https://fhir-ehr-code.cerner.com/oauth2/token",
        fhir_base="https://fhir-ehr-code.cerner.com/r4/ec2458f2-1e24-41c8-b71b-0e701af7583d",
    )


def test_discover_caches_endpoints(monkeypatch, cerner_env):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        assert request.url.path.endswith("/.well-known/smart-configuration")
        return httpx.Response(
            200,
            json={
                "authorization_endpoint": "https://fhir-ehr-code.cerner.com/oauth2/authorize",
                "token_endpoint": "https://fhir-ehr-code.cerner.com/oauth2/token",
            },
        )

    real_client = httpx.Client
    monkeypatch.setattr(
        "docstats.ehr.cerner.httpx.Client",
        lambda *a, **kw: real_client(*a, transport=_mock_transport(handler), **kw),
    )
    a = cerner.discover()
    b = cerner.discover()
    assert calls["n"] == 1
    assert a is b
    assert a.token_endpoint == "https://fhir-ehr-code.cerner.com/oauth2/token"
    # fhir_base is derived from the env tenant ID, not from the discovery payload.
    assert "ec2458f2" in a.fhir_base


def test_authorize_url_has_no_aud_param(monkeypatch, cerner_env):
    """Cerner does not require aud in the authorize request."""
    monkeypatch.setattr("docstats.ehr.cerner.discover", lambda **_: _fake_endpoints())
    url = cerner.build_authorize_url(state="s1", code_challenge="c1", scope="openid")
    assert url.startswith("https://fhir-ehr-code.cerner.com/oauth2/authorize?")
    assert "client_id=cerner-client" in url
    assert "code_challenge=c1" in url
    assert "code_challenge_method=S256" in url
    assert "state=s1" in url
    assert "aud=" not in url


def test_exchange_code_uses_basic_auth(monkeypatch, cerner_env):
    monkeypatch.setattr("docstats.ehr.cerner.discover", lambda **_: _fake_endpoints())

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization", "").startswith("Basic ")
        body = request.content.decode("utf-8")
        assert "grant_type=authorization_code" in body
        assert "code=THE_CODE" in body
        assert "code_verifier=THE_VERIFIER" in body
        return httpx.Response(
            200,
            json={
                "access_token": "AT",
                "refresh_token": "RT",
                "expires_in": 3600,
                "scope": "openid",
                "patient": "PAT-456",
            },
        )

    real_client = httpx.Client
    monkeypatch.setattr(
        "docstats.ehr.cerner.httpx.Client",
        lambda *a, **kw: real_client(*a, transport=_mock_transport(handler), **kw),
    )
    tok = cerner.exchange_code(code="THE_CODE", code_verifier="THE_VERIFIER")
    assert tok.access_token == "AT"
    assert tok.refresh_token == "RT"
    assert tok.patient_fhir_id == "PAT-456"
    assert tok.expires_in == 3600


def test_exchange_code_missing_access_token_raises(monkeypatch, cerner_env):
    monkeypatch.setattr("docstats.ehr.cerner.discover", lambda **_: _fake_endpoints())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": "invalid_grant"})

    real_client = httpx.Client
    monkeypatch.setattr(
        "docstats.ehr.cerner.httpx.Client",
        lambda *a, **kw: real_client(*a, transport=_mock_transport(handler), **kw),
    )
    with pytest.raises(CernerError):
        cerner.exchange_code(code="x", code_verifier="y")


def test_refresh_returns_new_token(monkeypatch, cerner_env):
    monkeypatch.setattr("docstats.ehr.cerner.discover", lambda **_: _fake_endpoints())

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        assert "grant_type=refresh_token" in body
        assert "refresh_token=OLD_RT" in body
        return httpx.Response(
            200,
            json={"access_token": "NEW_AT", "refresh_token": "NEW_RT", "expires_in": 900},
        )

    real_client = httpx.Client
    monkeypatch.setattr(
        "docstats.ehr.cerner.httpx.Client",
        lambda *a, **kw: real_client(*a, transport=_mock_transport(handler), **kw),
    )
    tok = cerner.refresh(refresh_token="OLD_RT")
    assert tok.access_token == "NEW_AT"
    assert tok.refresh_token == "NEW_RT"
    assert tok.expires_in == 900


def test_refresh_non_2xx_raises(monkeypatch, cerner_env):
    monkeypatch.setattr("docstats.ehr.cerner.discover", lambda **_: _fake_endpoints())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    real_client = httpx.Client
    monkeypatch.setattr(
        "docstats.ehr.cerner.httpx.Client",
        lambda *a, **kw: real_client(*a, transport=_mock_transport(handler), **kw),
    )
    with pytest.raises(CernerError):
        cerner.refresh(refresh_token="BAD")


def test_fetch_patient_uses_bearer(monkeypatch, cerner_env):
    monkeypatch.setattr("docstats.ehr.cerner.discover", lambda **_: _fake_endpoints())

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer THE_AT"
        assert str(request.url).endswith("/Patient/PAT-456")
        return httpx.Response(200, json={"resourceType": "Patient", "id": "PAT-456"})

    real_client = httpx.Client
    monkeypatch.setattr(
        "docstats.ehr.cerner.httpx.Client",
        lambda *a, **kw: real_client(*a, transport=_mock_transport(handler), **kw),
    )
    res = cerner.fetch_patient(access_token="THE_AT", patient_fhir_id="PAT-456")
    assert res["resourceType"] == "Patient"


def test_fetch_medications_uses_medication_request(monkeypatch, cerner_env):
    """Cerner medications endpoint queries MedicationRequest, not MedicationStatement."""
    monkeypatch.setattr("docstats.ehr.cerner.discover", lambda **_: _fake_endpoints())
    received: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        received["path"] = request.url.path
        received["params"] = dict(request.url.params)
        return httpx.Response(200, json={"resourceType": "Bundle", "entry": []})

    real_client = httpx.Client
    monkeypatch.setattr(
        "docstats.ehr.cerner.httpx.Client",
        lambda *a, **kw: real_client(*a, transport=_mock_transport(handler), **kw),
    )
    cerner.fetch_medications(access_token="T", patient_fhir_id="PAT-1")
    assert "MedicationRequest" in received["path"]
    assert received["params"]["patient"] == "PAT-1"
    assert received["params"]["status"] == "active"


def test_fetch_document_content_relative_url(monkeypatch, cerner_env):
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).startswith("https://fhir-ehr-code.cerner.com/r4/")
        assert "Binary/abc" in str(request.url)
        return httpx.Response(200, content=b"%PDF-1.4", headers={"content-type": "application/pdf"})

    real_client = httpx.Client
    monkeypatch.setattr(
        "docstats.ehr.cerner.httpx.Client",
        lambda *a, **kw: real_client(*a, transport=_mock_transport(handler), **kw),
    )
    data, mime = cerner.fetch_document_content(
        "Binary/abc",
        access_token="T",
        fhir_base="https://fhir-ehr-code.cerner.com/r4/ec2458f2",
    )
    assert data[:4] == b"%PDF"
    assert mime == "application/pdf"


def test_fetch_document_content_absolute_url(monkeypatch, cerner_env):
    received_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        received_urls.append(str(request.url))
        return httpx.Response(200, content=b"data", headers={"content-type": "image/jpeg"})

    real_client = httpx.Client
    monkeypatch.setattr(
        "docstats.ehr.cerner.httpx.Client",
        lambda *a, **kw: real_client(*a, transport=_mock_transport(handler), **kw),
    )
    cerner.fetch_document_content(
        "https://cdn.cerner.com/file.pdf",
        access_token="T",
        fhir_base="https://should-not-be-used.test",
    )
    assert received_urls[0] == "https://cdn.cerner.com/file.pdf"


def test_write_service_request_returns_id_from_body(monkeypatch, cerner_env):
    monkeypatch.setattr("docstats.ehr.cerner.discover", lambda **_: _fake_endpoints())

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        assert body["resourceType"] == "ServiceRequest"
        assert body["subject"]["reference"] == "Patient/PAT-1"
        assert any(
            i["system"] == "urn:docstats:referral" and i["value"] == "99"
            for i in body["identifier"]
        )
        return httpx.Response(201, json={"resourceType": "ServiceRequest", "id": "SR-C01"})

    real_client = httpx.Client
    monkeypatch.setattr(
        "docstats.ehr.cerner.httpx.Client",
        lambda *a, **kw: real_client(*a, transport=_mock_transport(handler), **kw),
    )
    sr_id = cerner.write_service_request(
        access_token="T",
        patient_fhir_id="PAT-1",
        referral_id=99,
        specialty_desc="Neurology",
        reason="Headache eval",
        requesting_provider_name="Dr. Jones",
    )
    assert sr_id == "SR-C01"


def test_write_service_request_id_from_location_header(monkeypatch, cerner_env):
    monkeypatch.setattr("docstats.ehr.cerner.discover", lambda **_: _fake_endpoints())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            json={"resourceType": "ServiceRequest"},
            headers={"Location": "https://fhir-ehr-code.cerner.com/r4/ec2/ServiceRequest/SR-C99"},
        )

    real_client = httpx.Client
    monkeypatch.setattr(
        "docstats.ehr.cerner.httpx.Client",
        lambda *a, **kw: real_client(*a, transport=_mock_transport(handler), **kw),
    )
    sr_id = cerner.write_service_request(
        access_token="T",
        patient_fhir_id="P",
        referral_id=7,
        specialty_desc=None,
        reason=None,
        requesting_provider_name=None,
    )
    assert sr_id == "SR-C99"


def test_write_service_request_no_id_raises(monkeypatch, cerner_env):
    monkeypatch.setattr("docstats.ehr.cerner.discover", lambda **_: _fake_endpoints())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"resourceType": "ServiceRequest"})

    real_client = httpx.Client
    monkeypatch.setattr(
        "docstats.ehr.cerner.httpx.Client",
        lambda *a, **kw: real_client(*a, transport=_mock_transport(handler), **kw),
    )
    with pytest.raises(CernerError):
        cerner.write_service_request(
            access_token="T",
            patient_fhir_id="P",
            referral_id=1,
            specialty_desc=None,
            reason=None,
            requesting_provider_name=None,
        )


def test_cerner_error_is_subclass_of_ehr_error():
    """CernerError must inherit EHRError so vendor-agnostic catch blocks work."""
    from docstats.ehr.registry import EHRError

    assert issubclass(CernerError, EHRError)


def test_cerner_registered_in_registry():
    from docstats.ehr import registry

    assert "cerner_oauth" in registry.list_vendors()
    mod = registry.get("cerner_oauth")
    assert hasattr(mod, "exchange_code")
    assert hasattr(mod, "fetch_patient")
    assert hasattr(mod, "fetch_medications")
