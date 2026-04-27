"""Epic SMART-on-FHIR client tests with mocked httpx."""

from __future__ import annotations

import httpx
import pytest

from docstats.ehr import epic
from docstats.ehr.epic import EpicError


@pytest.fixture(autouse=True)
def reset_discovery_cache():
    epic._DISCOVERY_CACHE.clear()
    yield
    epic._DISCOVERY_CACHE.clear()


@pytest.fixture
def epic_env(monkeypatch):
    monkeypatch.setenv("EPIC_CLIENT_ID", "fake-client")
    monkeypatch.setenv("EPIC_CLIENT_SECRET", "fake-secret")
    monkeypatch.setenv("EPIC_REDIRECT_URI", "https://referme.help/ehr/callback/epic")
    monkeypatch.setenv("EPIC_SANDBOX_BASE_URL", "https://fake-epic.test")


def _mock_transport(handler):
    return httpx.MockTransport(handler)


def test_discover_caches_endpoints(monkeypatch, epic_env):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        assert request.url.path.endswith("/.well-known/smart-configuration")
        return httpx.Response(
            200,
            json={
                "authorization_endpoint": "https://fake-epic.test/oauth2/authorize",
                "token_endpoint": "https://fake-epic.test/oauth2/token",
                "issuer": "https://fake-epic.test/api/FHIR/R4",
            },
        )

    real_client = httpx.Client
    monkeypatch.setattr(
        "docstats.ehr.epic.httpx.Client",
        lambda *a, **kw: real_client(*a, transport=_mock_transport(handler), **kw),
    )
    a = epic.discover()
    b = epic.discover()
    assert calls["n"] == 1
    assert a is b
    assert a.token_endpoint == "https://fake-epic.test/oauth2/token"
    # Regression-pin: discovery payload's `issuer` is the OAuth issuer, not
    # the FHIR base. fhir_base must come from the configured base URL so
    # Patient.read calls hit the right place.
    assert a.fhir_base == "https://fake-epic.test"


def test_authorize_url_includes_pkce_and_aud(monkeypatch, epic_env):
    monkeypatch.setattr(
        "docstats.ehr.epic.discover",
        lambda **_: epic.EpicEndpoints(
            authorize_endpoint="https://fake-epic.test/oauth2/authorize",
            token_endpoint="https://fake-epic.test/oauth2/token",
            fhir_base="https://fake-epic.test/api/FHIR/R4",
        ),
    )
    url = epic.build_authorize_url(
        state="s1", code_challenge="c1", scope="openid fhirUser launch/patient"
    )
    assert url.startswith("https://fake-epic.test/oauth2/authorize?")
    assert "client_id=fake-client" in url
    assert "code_challenge=c1" in url
    assert "code_challenge_method=S256" in url
    assert "aud=" in url
    assert "state=s1" in url


def test_exchange_code_uses_basic_auth(monkeypatch, epic_env):
    monkeypatch.setattr(
        "docstats.ehr.epic.discover",
        lambda **_: epic.EpicEndpoints(
            authorize_endpoint="x",
            token_endpoint="https://fake-epic.test/oauth2/token",
            fhir_base="x",
        ),
    )

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
                "patient": "PAT-123",
                "id_token": "IT",
            },
        )

    real_client = httpx.Client
    monkeypatch.setattr(
        "docstats.ehr.epic.httpx.Client",
        lambda *a, **kw: real_client(*a, transport=_mock_transport(handler), **kw),
    )
    tok = epic.exchange_code(code="THE_CODE", code_verifier="THE_VERIFIER")
    assert tok.access_token == "AT"
    assert tok.refresh_token == "RT"
    assert tok.patient_fhir_id == "PAT-123"
    assert tok.expires_in == 3600


def test_exchange_code_missing_access_token_raises(monkeypatch, epic_env):
    monkeypatch.setattr(
        "docstats.ehr.epic.discover",
        lambda **_: epic.EpicEndpoints(
            authorize_endpoint="x",
            token_endpoint="https://fake-epic.test/oauth2/token",
            fhir_base="x",
        ),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": "invalid_grant"})

    real_client = httpx.Client
    monkeypatch.setattr(
        "docstats.ehr.epic.httpx.Client",
        lambda *a, **kw: real_client(*a, transport=_mock_transport(handler), **kw),
    )
    with pytest.raises(EpicError):
        epic.exchange_code(code="x", code_verifier="y")


def test_fetch_patient_uses_bearer(monkeypatch, epic_env):
    monkeypatch.setattr(
        "docstats.ehr.epic.discover",
        lambda **_: epic.EpicEndpoints(
            authorize_endpoint="x",
            token_endpoint="x",
            fhir_base="https://fake-epic.test/api/FHIR/R4",
        ),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer THE_AT"
        assert str(request.url).endswith("/Patient/PAT-123")
        return httpx.Response(200, json={"resourceType": "Patient", "id": "PAT-123"})

    real_client = httpx.Client
    monkeypatch.setattr(
        "docstats.ehr.epic.httpx.Client",
        lambda *a, **kw: real_client(*a, transport=_mock_transport(handler), **kw),
    )
    res = epic.fetch_patient(access_token="THE_AT", patient_fhir_id="PAT-123")
    assert res["resourceType"] == "Patient"


def test_pkce_pair_returns_distinct_strings():
    v, c = epic.make_pkce_pair()
    assert v
    assert c
    assert v != c
    # base64url has no padding/+//
    assert "=" not in c
    assert "+" not in c and "/" not in c
