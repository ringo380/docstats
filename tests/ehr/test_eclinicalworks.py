"""eClinicalWorks SMART-on-FHIR client tests with mocked httpx."""

from __future__ import annotations

import base64

import httpx
import pytest

from docstats.ehr import eclinicalworks as ecw
from docstats.ehr.eclinicalworks import ECWError


@pytest.fixture(autouse=True)
def reset_discovery_cache():
    ecw._DISCOVERY_CACHE.clear()
    yield
    ecw._DISCOVERY_CACHE.clear()


@pytest.fixture
def ecw_env(monkeypatch):
    monkeypatch.setenv("ECW_CLIENT_ID", "ecw-client")
    monkeypatch.setenv("ECW_CLIENT_SECRET", "ecw-secret-shhh")
    monkeypatch.setenv("ECW_REDIRECT_URI", "https://referme.help/ehr/callback/ecw")
    monkeypatch.setenv(
        "ECW_SANDBOX_FHIR_BASE", "https://fhir.eclinicalworks.com/fhirr4/rest/r4/api"
    )


def _mock_transport(handler):
    return httpx.MockTransport(handler)


def _fake_endpoints():
    return ecw.ECWEndpoints(
        authorize_endpoint="https://oauth.eclinicalworks.com/oauth/authorize",
        token_endpoint="https://oauth.eclinicalworks.com/oauth/token",
        fhir_base="https://fhir.eclinicalworks.com/fhirr4/rest/r4/api",
    )


def test_default_fhir_base_fail_closed_when_unset(monkeypatch):
    """ECW_SANDBOX_FHIR_BASE must fail-closed — no hardcoded fallback."""
    monkeypatch.delenv("ECW_SANDBOX_FHIR_BASE", raising=False)
    with pytest.raises(ECWError, match="ECW_SANDBOX_FHIR_BASE"):
        ecw._default_fhir_base()


def test_client_secret_fail_closed_when_unset(monkeypatch, ecw_env):
    monkeypatch.delenv("ECW_CLIENT_SECRET", raising=False)
    with pytest.raises(ECWError, match="ECW_CLIENT_SECRET"):
        ecw._client_secret()


def test_basic_auth_header_format(ecw_env):
    header = ecw._basic_auth_header()
    assert header.startswith("Basic ")
    decoded = base64.b64decode(header[len("Basic ") :]).decode()
    assert decoded == "ecw-client:ecw-secret-shhh"


def test_discover_caches_endpoints(monkeypatch, ecw_env):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        assert request.url.path.endswith("/.well-known/smart-configuration")
        return httpx.Response(
            200,
            json={
                "authorization_endpoint": "https://oauth.eclinicalworks.com/oauth/authorize",
                "token_endpoint": "https://oauth.eclinicalworks.com/oauth/token",
            },
        )

    real_client = httpx.Client
    monkeypatch.setattr(
        "docstats.ehr.eclinicalworks.httpx.Client",
        lambda *a, **kw: real_client(*a, transport=_mock_transport(handler), **kw),
    )
    a = ecw.discover()
    b = ecw.discover()
    assert calls["n"] == 1
    assert a is b
    assert a.token_endpoint == "https://oauth.eclinicalworks.com/oauth/token"
    # fhir_base comes from env, NOT from the discovery payload (which omits it).
    assert a.fhir_base == "https://fhir.eclinicalworks.com/fhirr4/rest/r4/api"


def test_discover_force_refresh_bypasses_cache(monkeypatch, ecw_env):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            200,
            json={
                "authorization_endpoint": "https://oauth.eclinicalworks.com/oauth/authorize",
                "token_endpoint": "https://oauth.eclinicalworks.com/oauth/token",
            },
        )

    real_client = httpx.Client
    monkeypatch.setattr(
        "docstats.ehr.eclinicalworks.httpx.Client",
        lambda *a, **kw: real_client(*a, transport=_mock_transport(handler), **kw),
    )
    ecw.discover()
    ecw.discover(force_refresh=True)
    assert calls["n"] == 2


def test_authorize_url_includes_aud_param(monkeypatch, ecw_env):
    """eCW authorize URL must include aud = fhir_base."""
    monkeypatch.setattr("docstats.ehr.eclinicalworks.discover", lambda **_: _fake_endpoints())
    url = ecw.build_authorize_url(state="s1", code_challenge="c1", scope="openid fhirUser")
    assert url.startswith("https://oauth.eclinicalworks.com/oauth/authorize?")
    assert "client_id=ecw-client" in url
    assert "code_challenge=c1" in url
    assert "code_challenge_method=S256" in url
    assert "state=s1" in url
    assert "aud=https%3A%2F%2Ffhir.eclinicalworks.com%2Ffhirr4%2Frest%2Fr4%2Fapi" in url


def test_ehr_launch_authorize_url_includes_launch_token(monkeypatch, ecw_env):
    monkeypatch.setattr(
        "docstats.ehr.eclinicalworks.discover",
        lambda **_: _fake_endpoints(),
    )
    url = ecw.build_ehr_launch_authorize_url(
        state="s1",
        code_challenge="c1",
        scope="openid fhirUser launch",
        launch_token="LAUNCH_XYZ",
        iss_override="https://fhir.eclinicalworks.com/fhirr4/rest/r4/api",
    )
    assert "launch=LAUNCH_XYZ" in url
    assert "aud=https%3A%2F%2Ffhir.eclinicalworks.com%2Ffhirr4" in url


def test_exchange_code_uses_basic_auth_not_client_id_in_body(monkeypatch, ecw_env):
    """Confidential client: Basic auth header present, client_id NOT in form body.

    This is the key difference from Cerner (which is public/PKCE-only and
    sends client_id in the form body without any Basic auth header).
    """
    monkeypatch.setattr("docstats.ehr.eclinicalworks.discover", lambda **_: _fake_endpoints())

    def handler(request: httpx.Request) -> httpx.Response:
        # Basic auth header MUST be present and decode to client_id:client_secret.
        auth = request.headers.get("authorization", "")
        assert auth.startswith("Basic ")
        decoded = base64.b64decode(auth[len("Basic ") :]).decode()
        assert decoded == "ecw-client:ecw-secret-shhh"

        # client_id MUST NOT be in the form body — it's already in the header.
        body = request.content.decode("utf-8")
        assert "client_id=" not in body
        assert "grant_type=authorization_code" in body
        assert "code=THE_CODE" in body
        assert "code_verifier=THE_VERIFIER" in body
        return httpx.Response(
            200,
            json={
                "access_token": "AT",
                "refresh_token": "RT",
                "expires_in": 3600,
                "scope": "openid fhirUser",
                "patient": "PAT-789",
            },
        )

    real_client = httpx.Client
    monkeypatch.setattr(
        "docstats.ehr.eclinicalworks.httpx.Client",
        lambda *a, **kw: real_client(*a, transport=_mock_transport(handler), **kw),
    )
    tok = ecw.exchange_code(code="THE_CODE", code_verifier="THE_VERIFIER")
    assert tok.access_token == "AT"
    assert tok.refresh_token == "RT"
    assert tok.patient_fhir_id == "PAT-789"
    assert tok.expires_in == 3600


def test_exchange_code_missing_access_token_raises(monkeypatch, ecw_env):
    monkeypatch.setattr("docstats.ehr.eclinicalworks.discover", lambda **_: _fake_endpoints())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": "invalid_grant"})

    real_client = httpx.Client
    monkeypatch.setattr(
        "docstats.ehr.eclinicalworks.httpx.Client",
        lambda *a, **kw: real_client(*a, transport=_mock_transport(handler), **kw),
    )
    with pytest.raises(ECWError):
        ecw.exchange_code(code="x", code_verifier="y")


def test_refresh_uses_basic_auth(monkeypatch, ecw_env):
    monkeypatch.setattr("docstats.ehr.eclinicalworks.discover", lambda **_: _fake_endpoints())

    def handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("authorization", "")
        assert auth.startswith("Basic ")
        body = request.content.decode()
        assert "grant_type=refresh_token" in body
        assert "refresh_token=OLD_RT" in body
        # client_id NOT in body — it's in the Basic header.
        assert "client_id=" not in body
        return httpx.Response(
            200,
            json={"access_token": "NEW_AT", "refresh_token": "NEW_RT", "expires_in": 900},
        )

    real_client = httpx.Client
    monkeypatch.setattr(
        "docstats.ehr.eclinicalworks.httpx.Client",
        lambda *a, **kw: real_client(*a, transport=_mock_transport(handler), **kw),
    )
    tok = ecw.refresh(refresh_token="OLD_RT")
    assert tok.access_token == "NEW_AT"
    assert tok.refresh_token == "NEW_RT"
    assert tok.expires_in == 900


def test_refresh_preserves_old_rt_when_response_omits(monkeypatch, ecw_env):
    monkeypatch.setattr("docstats.ehr.eclinicalworks.discover", lambda **_: _fake_endpoints())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"access_token": "NEW_AT", "expires_in": 900},
        )

    real_client = httpx.Client
    monkeypatch.setattr(
        "docstats.ehr.eclinicalworks.httpx.Client",
        lambda *a, **kw: real_client(*a, transport=_mock_transport(handler), **kw),
    )
    tok = ecw.refresh(refresh_token="OLD_RT")
    assert tok.refresh_token == "OLD_RT"


def test_refresh_non_2xx_raises(monkeypatch, ecw_env):
    monkeypatch.setattr("docstats.ehr.eclinicalworks.discover", lambda **_: _fake_endpoints())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    real_client = httpx.Client
    monkeypatch.setattr(
        "docstats.ehr.eclinicalworks.httpx.Client",
        lambda *a, **kw: real_client(*a, transport=_mock_transport(handler), **kw),
    )
    with pytest.raises(ECWError):
        ecw.refresh(refresh_token="BAD")


def test_fetch_patient_uses_bearer(monkeypatch, ecw_env):
    monkeypatch.setattr("docstats.ehr.eclinicalworks.discover", lambda **_: _fake_endpoints())

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer THE_AT"
        assert str(request.url).endswith("/Patient/PAT-789")
        return httpx.Response(200, json={"resourceType": "Patient", "id": "PAT-789"})

    real_client = httpx.Client
    monkeypatch.setattr(
        "docstats.ehr.eclinicalworks.httpx.Client",
        lambda *a, **kw: real_client(*a, transport=_mock_transport(handler), **kw),
    )
    res = ecw.fetch_patient(access_token="THE_AT", patient_fhir_id="PAT-789")
    assert res["resourceType"] == "Patient"


def test_fetch_patient_iss_override_routes_to_tenant_base(monkeypatch, ecw_env):
    """Multi-tenant: fetch_patient hits the FHIR base supplied by iss_override,
    not the env default. eCW practices each have their own FHIR base."""
    captured: dict[str, str | None] = {"base": None}

    def fake_discover(*, base_url_override=None, force_refresh=False):
        captured["base"] = base_url_override
        return ecw.ECWEndpoints(
            authorize_endpoint="https://t.example/authorize",
            token_endpoint="https://t.example/token",
            fhir_base=(base_url_override or "https://default.example").rstrip("/"),
        )

    monkeypatch.setattr("docstats.ehr.eclinicalworks.discover", fake_discover)

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).startswith("https://practice-7.example")
        return httpx.Response(200, json={"resourceType": "Patient", "id": "P"})

    real_client = httpx.Client
    monkeypatch.setattr(
        "docstats.ehr.eclinicalworks.httpx.Client",
        lambda *a, **kw: real_client(*a, transport=_mock_transport(handler), **kw),
    )

    ecw.fetch_patient(
        access_token="T", patient_fhir_id="P", iss_override="https://practice-7.example"
    )
    assert captured["base"] == "https://practice-7.example"


def test_refresh_iss_override_routes_to_tenant_base(monkeypatch, ecw_env):
    """Refresh must hit the same tenant the connection was minted against."""
    captured: dict[str, str | None] = {"base": None}

    def fake_discover(*, base_url_override=None, force_refresh=False):
        captured["base"] = base_url_override
        return ecw.ECWEndpoints(
            authorize_endpoint="https://t.example/authorize",
            token_endpoint="https://practice-9.example/token",
            fhir_base=base_url_override or "https://default.example",
        )

    monkeypatch.setattr("docstats.ehr.eclinicalworks.discover", fake_discover)

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://practice-9.example/token"
        return httpx.Response(
            200,
            json={"access_token": "NEW_AT", "refresh_token": "NEW_RT", "expires_in": 3600},
        )

    real_client = httpx.Client
    monkeypatch.setattr(
        "docstats.ehr.eclinicalworks.httpx.Client",
        lambda *a, **kw: real_client(*a, transport=_mock_transport(handler), **kw),
    )

    tok = ecw.refresh("OLD_RT", iss_override="https://practice-9.example")
    assert tok.access_token == "NEW_AT"
    assert captured["base"] == "https://practice-9.example"


def test_reset_discovery_cache_clears_in_process_state(monkeypatch, ecw_env):
    ecw._DISCOVERY_CACHE["https://x.example"] = (_fake_endpoints(), 999_999_999.0)
    assert ecw._DISCOVERY_CACHE
    ecw.reset_discovery_cache()
    assert ecw._DISCOVERY_CACHE == {}


def test_fetch_medications_uses_medication_request(monkeypatch, ecw_env):
    """eCW uses MedicationRequest like Cerner — wrong resource → 404."""
    monkeypatch.setattr("docstats.ehr.eclinicalworks.discover", lambda **_: _fake_endpoints())
    received: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        received["path"] = request.url.path
        received["params"] = dict(request.url.params)
        return httpx.Response(200, json={"resourceType": "Bundle", "entry": []})

    real_client = httpx.Client
    monkeypatch.setattr(
        "docstats.ehr.eclinicalworks.httpx.Client",
        lambda *a, **kw: real_client(*a, transport=_mock_transport(handler), **kw),
    )
    ecw.fetch_medications(access_token="T", patient_fhir_id="PAT-1")
    assert "MedicationRequest" in received["path"]
    assert "MedicationStatement" not in received["path"]
    assert received["params"]["patient"] == "PAT-1"
    assert received["params"]["status"] == "active"


def test_fetch_conditions_filters_active(monkeypatch, ecw_env):
    monkeypatch.setattr("docstats.ehr.eclinicalworks.discover", lambda **_: _fake_endpoints())
    received: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        received["params"] = dict(request.url.params)
        return httpx.Response(
            200,
            json={
                "resourceType": "Bundle",
                "entry": [{"resource": {"resourceType": "Condition", "id": "C1"}}],
            },
        )

    real_client = httpx.Client
    monkeypatch.setattr(
        "docstats.ehr.eclinicalworks.httpx.Client",
        lambda *a, **kw: real_client(*a, transport=_mock_transport(handler), **kw),
    )
    out = ecw.fetch_conditions(access_token="T", patient_fhir_id="P")
    assert received["params"]["clinical-status"] == "active"
    assert len(out) == 1
    assert out[0]["id"] == "C1"


def test_fetch_document_content_relative_url(monkeypatch, ecw_env):
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).startswith("https://fhir.eclinicalworks.com/fhirr4/")
        assert "Binary/abc" in str(request.url)
        return httpx.Response(200, content=b"%PDF-1.4", headers={"content-type": "application/pdf"})

    real_client = httpx.Client
    monkeypatch.setattr(
        "docstats.ehr.eclinicalworks.httpx.Client",
        lambda *a, **kw: real_client(*a, transport=_mock_transport(handler), **kw),
    )
    data, mime = ecw.fetch_document_content(
        "Binary/abc",
        access_token="T",
        fhir_base="https://fhir.eclinicalworks.com/fhirr4/rest/r4/api",
    )
    assert data[:4] == b"%PDF"
    assert mime == "application/pdf"


def test_write_service_request_returns_id_from_body(monkeypatch, ecw_env):
    monkeypatch.setattr("docstats.ehr.eclinicalworks.discover", lambda **_: _fake_endpoints())

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        assert body["resourceType"] == "ServiceRequest"
        assert body["intent"] == "referral"
        assert body["subject"]["reference"] == "Patient/PAT-1"
        assert any(
            i["system"] == "urn:docstats:referral" and i["value"] == "42"
            for i in body["identifier"]
        )
        # FHIR R4 ServiceRequest uses `performerType` (single CodeableConcept),
        # NOT `specialty` (which doesn't exist on the resource).
        assert "specialty" not in body
        assert body["performerType"] == {"text": "Cardiology"}
        return httpx.Response(201, json={"resourceType": "ServiceRequest", "id": "SR-E42"})

    real_client = httpx.Client
    monkeypatch.setattr(
        "docstats.ehr.eclinicalworks.httpx.Client",
        lambda *a, **kw: real_client(*a, transport=_mock_transport(handler), **kw),
    )
    sr_id = ecw.write_service_request(
        access_token="T",
        patient_fhir_id="PAT-1",
        referral_id=42,
        specialty_desc="Cardiology",
        reason="Chest pain workup",
        requesting_provider_name="Dr. Smith",
    )
    assert sr_id == "SR-E42"


def test_write_service_request_id_from_location_header(monkeypatch, ecw_env):
    monkeypatch.setattr("docstats.ehr.eclinicalworks.discover", lambda **_: _fake_endpoints())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            json={"resourceType": "ServiceRequest"},
            headers={
                "Location": "https://fhir.eclinicalworks.com/fhirr4/rest/r4/api/ServiceRequest/SR-E99"
            },
        )

    real_client = httpx.Client
    monkeypatch.setattr(
        "docstats.ehr.eclinicalworks.httpx.Client",
        lambda *a, **kw: real_client(*a, transport=_mock_transport(handler), **kw),
    )
    sr_id = ecw.write_service_request(
        access_token="T",
        patient_fhir_id="P",
        referral_id=7,
        specialty_desc=None,
        reason=None,
        requesting_provider_name=None,
    )
    assert sr_id == "SR-E99"


def test_redact_strips_token_fields():
    payload = {
        "access_token": "AT",
        "refresh_token": "RT",
        "id_token": "IT",
        "code": "C",
        "client_secret": "S",
        "scope": "openid",
        "expires_in": 3600,
    }
    redacted = ecw._redact(payload)
    assert isinstance(redacted, dict)
    assert redacted["access_token"] == "***"
    assert redacted["refresh_token"] == "***"
    assert redacted["id_token"] == "***"
    assert redacted["code"] == "***"
    assert redacted["client_secret"] == "***"
    # Non-token fields preserved.
    assert redacted["scope"] == "openid"
    assert redacted["expires_in"] == 3600


def test_ecw_error_is_subclass_of_ehr_error():
    """ECWError must inherit EHRError so vendor-agnostic catch blocks work."""
    from docstats.ehr.registry import EHRError

    assert issubclass(ECWError, EHRError)


def test_ecw_registered_in_registry():
    # Ensure side-effect import has registered the module.
    from docstats.ehr import eclinicalworks  # noqa: F401
    from docstats.ehr import registry

    assert "ecw_smart" in registry.list_vendors()
    mod = registry.get("ecw_smart")
    assert hasattr(mod, "exchange_code")
    assert hasattr(mod, "refresh")
    assert hasattr(mod, "fetch_patient")
    assert hasattr(mod, "fetch_medications")
    assert hasattr(mod, "fetch_document_references")
    assert hasattr(mod, "write_service_request")


def test_ecw_in_domain_vendors_set():
    from docstats.domain.ehr import EHR_VENDORS

    assert "ecw_smart" in EHR_VENDORS
