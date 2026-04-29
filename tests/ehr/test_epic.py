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


# ---------------------------------------------------------------------------
# refresh
# ---------------------------------------------------------------------------


def test_refresh_returns_new_token(monkeypatch, epic_env):
    monkeypatch.setattr(
        "docstats.ehr.epic.discover",
        lambda **_: epic.EpicEndpoints(
            authorize_endpoint="x",
            token_endpoint="https://fake-epic.test/oauth2/token",
            fhir_base="x",
        ),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        assert "grant_type=refresh_token" in body
        assert "refresh_token=OLD_RT" in body
        return httpx.Response(
            200,
            json={
                "access_token": "NEW_AT",
                "refresh_token": "NEW_RT",
                "expires_in": 900,
            },
        )

    real_client = httpx.Client
    monkeypatch.setattr(
        "docstats.ehr.epic.httpx.Client",
        lambda *a, **kw: real_client(*a, transport=_mock_transport(handler), **kw),
    )
    tok = epic.refresh(refresh_token="OLD_RT")
    assert tok.access_token == "NEW_AT"
    assert tok.refresh_token == "NEW_RT"
    assert tok.expires_in == 900


def test_refresh_non_2xx_raises(monkeypatch, epic_env):
    monkeypatch.setattr(
        "docstats.ehr.epic.discover",
        lambda **_: epic.EpicEndpoints(
            authorize_endpoint="x",
            token_endpoint="https://fake-epic.test/oauth2/token",
            fhir_base="x",
        ),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    real_client = httpx.Client
    monkeypatch.setattr(
        "docstats.ehr.epic.httpx.Client",
        lambda *a, **kw: real_client(*a, transport=_mock_transport(handler), **kw),
    )
    with pytest.raises(EpicError):
        epic.refresh(refresh_token="BAD")


# ---------------------------------------------------------------------------
# fetch_document_content
# ---------------------------------------------------------------------------


def test_fetch_document_content_relative_url(monkeypatch, epic_env):
    """Relative URL must be resolved against fhir_base."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://fake-epic.test/api/FHIR/R4/Binary/abc"
        assert request.headers["authorization"] == "Bearer THE_AT"
        return httpx.Response(200, content=b"%PDF-1.4", headers={"content-type": "application/pdf"})

    real_client = httpx.Client
    monkeypatch.setattr(
        "docstats.ehr.epic.httpx.Client",
        lambda *a, **kw: real_client(*a, transport=_mock_transport(handler), **kw),
    )
    data, mime = epic.fetch_document_content(
        "Binary/abc",
        access_token="THE_AT",
        fhir_base="https://fake-epic.test/api/FHIR/R4",
    )
    assert data[:4] == b"%PDF"
    assert mime == "application/pdf"


def test_fetch_document_content_absolute_url(monkeypatch, epic_env):
    """Absolute URL used as-is regardless of fhir_base."""
    received_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        received_urls.append(str(request.url))
        return httpx.Response(200, content=b"data", headers={"content-type": "image/jpeg"})

    real_client = httpx.Client
    monkeypatch.setattr(
        "docstats.ehr.epic.httpx.Client",
        lambda *a, **kw: real_client(*a, transport=_mock_transport(handler), **kw),
    )
    epic.fetch_document_content(
        "https://cdn.example.com/file.jpg",
        access_token="T",
        fhir_base="https://should-not-be-used.test",
    )
    assert received_urls[0] == "https://cdn.example.com/file.jpg"


def test_fetch_document_content_non_2xx_raises(monkeypatch, epic_env):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "forbidden"})

    real_client = httpx.Client
    monkeypatch.setattr(
        "docstats.ehr.epic.httpx.Client",
        lambda *a, **kw: real_client(*a, transport=_mock_transport(handler), **kw),
    )
    with pytest.raises(EpicError):
        epic.fetch_document_content(
            "Binary/x", access_token="T", fhir_base="https://fake-epic.test"
        )


# ---------------------------------------------------------------------------
# write_service_request
# ---------------------------------------------------------------------------


def test_write_service_request_returns_id_from_body(monkeypatch, epic_env):
    monkeypatch.setattr(
        "docstats.ehr.epic.discover",
        lambda **_: epic.EpicEndpoints(
            authorize_endpoint="x",
            token_endpoint="x",
            fhir_base="https://fake-epic.test/api/FHIR/R4",
        ),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        assert body["resourceType"] == "ServiceRequest"
        assert body["subject"]["reference"] == "Patient/PAT-1"
        assert any(
            i["system"] == "urn:docstats:referral" and i["value"] == "42"
            for i in body["identifier"]
        )
        return httpx.Response(201, json={"resourceType": "ServiceRequest", "id": "SR-001"})

    real_client = httpx.Client
    monkeypatch.setattr(
        "docstats.ehr.epic.httpx.Client",
        lambda *a, **kw: real_client(*a, transport=_mock_transport(handler), **kw),
    )
    sr_id = epic.write_service_request(
        access_token="T",
        patient_fhir_id="PAT-1",
        referral_id=42,
        specialty_desc="Cardiology",
        reason="Chest pain eval",
        requesting_provider_name="Dr. Smith",
    )
    assert sr_id == "SR-001"


def test_write_service_request_id_from_location_header(monkeypatch, epic_env):
    monkeypatch.setattr(
        "docstats.ehr.epic.discover",
        lambda **_: epic.EpicEndpoints(
            authorize_endpoint="x",
            token_endpoint="x",
            fhir_base="https://fake-epic.test/api/FHIR/R4",
        ),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            json={"resourceType": "ServiceRequest"},
            headers={"Location": "https://fake-epic.test/api/FHIR/R4/ServiceRequest/SR-999"},
        )

    real_client = httpx.Client
    monkeypatch.setattr(
        "docstats.ehr.epic.httpx.Client",
        lambda *a, **kw: real_client(*a, transport=_mock_transport(handler), **kw),
    )
    sr_id = epic.write_service_request(
        access_token="T",
        patient_fhir_id="P",
        referral_id=7,
        specialty_desc=None,
        reason=None,
        requesting_provider_name=None,
    )
    assert sr_id == "SR-999"


def test_write_service_request_no_id_raises(monkeypatch, epic_env):
    monkeypatch.setattr(
        "docstats.ehr.epic.discover",
        lambda **_: epic.EpicEndpoints(
            authorize_endpoint="x",
            token_endpoint="x",
            fhir_base="https://fake-epic.test/api/FHIR/R4",
        ),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"resourceType": "ServiceRequest"})

    real_client = httpx.Client
    monkeypatch.setattr(
        "docstats.ehr.epic.httpx.Client",
        lambda *a, **kw: real_client(*a, transport=_mock_transport(handler), **kw),
    )
    with pytest.raises(EpicError):
        epic.write_service_request(
            access_token="T",
            patient_fhir_id="P",
            referral_id=1,
            specialty_desc=None,
            reason=None,
            requesting_provider_name=None,
        )


# ---------------------------------------------------------------------------
# fetch_conditions / medications / allergies / document_references
# (test via _fetch_fhir_bundle_entries stub)
# ---------------------------------------------------------------------------


def _bundle_response(resource_type: str, entries: list[dict]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "resourceType": "Bundle",
            "total": len(entries),
            "entry": [{"resource": e} for e in entries],
        },
    )


def test_fetch_conditions_passes_correct_params(monkeypatch, epic_env):
    monkeypatch.setattr(
        "docstats.ehr.epic.discover",
        lambda **_: epic.EpicEndpoints(
            authorize_endpoint="x",
            token_endpoint="x",
            fhir_base="https://fake-epic.test/api/FHIR/R4",
        ),
    )
    received: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        received["path"] = request.url.path
        received["params"] = dict(request.url.params)
        return _bundle_response("Condition", [{"resourceType": "Condition"}])

    real_client = httpx.Client
    monkeypatch.setattr(
        "docstats.ehr.epic.httpx.Client",
        lambda *a, **kw: real_client(*a, transport=_mock_transport(handler), **kw),
    )
    entries = epic.fetch_conditions(access_token="T", patient_fhir_id="PAT-1")
    assert entries[0]["resourceType"] == "Condition"
    assert received["params"]["patient"] == "PAT-1"
    assert received["params"]["clinical-status"] == "active"


def test_fetch_document_references_passes_correct_params(monkeypatch, epic_env):
    monkeypatch.setattr(
        "docstats.ehr.epic.discover",
        lambda **_: epic.EpicEndpoints(
            authorize_endpoint="x",
            token_endpoint="x",
            fhir_base="https://fake-epic.test/api/FHIR/R4",
        ),
    )
    received: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        received["params"] = dict(request.url.params)
        return _bundle_response("DocumentReference", [])

    real_client = httpx.Client
    monkeypatch.setattr(
        "docstats.ehr.epic.httpx.Client",
        lambda *a, **kw: real_client(*a, transport=_mock_transport(handler), **kw),
    )
    entries = epic.fetch_document_references(access_token="T", patient_fhir_id="PAT-X")
    assert entries == []
    assert received["params"]["status"] == "current"
