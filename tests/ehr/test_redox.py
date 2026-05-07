"""Redox aggregator client tests with mocked httpx + a fresh test keypair."""

from __future__ import annotations

import json

import httpx
import jwt as _jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from docstats.ehr import redox
from docstats.ehr.redox import RedoxConfigError, RedoxError


# ---------------------------------------------------------------------------
# Test fixtures: ephemeral RSA keypair + clean module state
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def test_keypair() -> tuple[str, str]:
    """Generate a small (2048-bit) RSA keypair for the test module.

    Returns ``(private_pem, public_pem)`` as PEM strings.
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private_pem, public_pem


@pytest.fixture(autouse=True)
def reset_token_cache():
    redox.reset_token_cache()
    yield
    redox.reset_token_cache()


@pytest.fixture
def redox_env(monkeypatch, test_keypair):
    private_pem, _ = test_keypair
    monkeypatch.setenv("REDOX_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("REDOX_KEY_ID", "test-kid")
    monkeypatch.setenv("REDOX_PRIVATE_KEY_PEM", private_pem)
    # Force defaults for token URL + FHIR base.
    monkeypatch.delenv("REDOX_TOKEN_URL", raising=False)
    monkeypatch.delenv("REDOX_FHIR_BASE", raising=False)
    monkeypatch.delenv("REDOX_FHIR_DESTINATION", raising=False)


def _mock_transport(handler):
    return httpx.MockTransport(handler)


def _patch_httpx(monkeypatch, handler):
    real_client = httpx.Client
    monkeypatch.setattr(
        "docstats.ehr.redox.httpx.Client",
        lambda *a, **kw: real_client(*a, transport=_mock_transport(handler), **kw),
    )


# ---------------------------------------------------------------------------
# JWT assertion + token mint
# ---------------------------------------------------------------------------


def test_build_client_assertion_has_correct_header_and_claims(redox_env, test_keypair):
    _, public_pem = test_keypair
    assertion = redox.build_client_assertion(now=1_750_000_000)
    header = _jwt.get_unverified_header(assertion)
    assert header == {"alg": "RS384", "kid": "test-kid", "typ": "JWT"}
    claims = _jwt.decode(
        assertion,
        public_pem,
        algorithms=["RS384"],
        audience="https://api.redoxengine.com/v2/auth/token",
        # Fixed historical ``now`` makes exp time-static; skip clock check.
        options={"verify_exp": False},
    )
    assert claims["iss"] == "test-client-id"
    assert claims["sub"] == "test-client-id"
    assert claims["aud"] == "https://api.redoxengine.com/v2/auth/token"
    assert claims["iat"] == 1_750_000_000
    assert claims["exp"] == 1_750_000_000 + 300
    assert "jti" in claims and len(claims["jti"]) >= 16


def test_build_client_assertion_jti_is_unique(redox_env):
    a = redox.build_client_assertion(now=1_750_000_000)
    b = redox.build_client_assertion(now=1_750_000_000)
    assert a != b
    a_claims = _jwt.decode(a, options={"verify_signature": False})
    b_claims = _jwt.decode(b, options={"verify_signature": False})
    assert a_claims["jti"] != b_claims["jti"]


def test_load_private_key_prefers_inline_env(monkeypatch, test_keypair):
    private_pem, _ = test_keypair
    monkeypatch.setenv("REDOX_PRIVATE_KEY_PEM", private_pem)
    monkeypatch.setenv("REDOX_PRIVATE_KEY_PATH", "/no/such/file.pem")
    # Should NOT raise FileNotFoundError — inline takes priority.
    assert redox._load_private_key().strip() == private_pem.strip()


def test_load_private_key_missing_raises(monkeypatch):
    monkeypatch.delenv("REDOX_PRIVATE_KEY_PEM", raising=False)
    monkeypatch.delenv("REDOX_PRIVATE_KEY_PATH", raising=False)
    with pytest.raises(RedoxConfigError):
        redox._load_private_key()


def test_request_access_token_uses_jwt_bearer_grant(monkeypatch, redox_env):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.content.decode()
        return httpx.Response(200, json={"access_token": "tok-1", "expires_in": 300})

    _patch_httpx(monkeypatch, handler)
    token = redox.request_access_token(scope="system/Patient.read")
    assert token == "tok-1"
    assert captured["url"] == "https://api.redoxengine.com/v2/auth/token"
    assert "grant_type=client_credentials" in captured["body"]
    assert (
        "client_assertion_type=urn%3Aietf%3Aparams%3Aoauth%3A"
        "client-assertion-type%3Ajwt-bearer" in captured["body"]
    )
    assert "client_assertion=" in captured["body"]
    assert "scope=system%2FPatient.read" in captured["body"]


def test_request_access_token_caches(monkeypatch, redox_env):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"access_token": "tok-2", "expires_in": 300})

    _patch_httpx(monkeypatch, handler)
    a = redox.request_access_token(scope="system/Patient.read")
    b = redox.request_access_token(scope="system/Patient.read")
    assert a == b == "tok-2"
    assert calls["n"] == 1


def test_request_access_token_force_refresh_bypasses_cache(monkeypatch, redox_env):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"access_token": f"tok-{calls['n']}", "expires_in": 300})

    _patch_httpx(monkeypatch, handler)
    redox.request_access_token(scope="system/Patient.read")
    second = redox.request_access_token(scope="system/Patient.read", force_refresh=True)
    assert calls["n"] == 2
    assert second == "tok-2"


def test_request_access_token_missing_field_raises(monkeypatch, redox_env):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"expires_in": 300})  # no access_token

    _patch_httpx(monkeypatch, handler)
    with pytest.raises(RedoxError):
        redox.request_access_token(scope="system/Patient.read")


# ---------------------------------------------------------------------------
# FHIR helpers — destination path is appended between base and resource
# ---------------------------------------------------------------------------


def test_find_patient_by_mrn_returns_id(monkeypatch, redox_env):
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/redox-fhir-sandbox/Development/Patient" in str(request.url)
        assert "identifier=urn%3Aoid%3A1.2.3%7CMRN42" in str(request.url)
        assert request.headers["Authorization"] == "Bearer tok-x"
        return httpx.Response(
            200,
            json={
                "resourceType": "Bundle",
                "entry": [{"resource": {"resourceType": "Patient", "id": "fhir-id-9"}}],
            },
        )

    _patch_httpx(monkeypatch, handler)
    pid = redox.find_patient_by_mrn(
        access_token="tok-x", mrn="MRN42", mrn_system="urn:oid:1.2.3"
    )
    assert pid == "fhir-id-9"


def test_find_patient_by_mrn_returns_none_on_miss(monkeypatch, redox_env):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"resourceType": "Bundle", "entry": []})

    _patch_httpx(monkeypatch, handler)
    assert redox.find_patient_by_mrn(access_token="tok-x", mrn="missing") is None


def test_find_patient_by_mrn_raises_on_multi_match(monkeypatch, redox_env):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "resourceType": "Bundle",
                "entry": [
                    {"resource": {"resourceType": "Patient", "id": "a"}},
                    {"resource": {"resourceType": "Patient", "id": "b"}},
                ],
            },
        )

    _patch_httpx(monkeypatch, handler)
    with pytest.raises(RedoxError):
        redox.find_patient_by_mrn(access_token="tok-x", mrn="ambiguous")


def test_fetch_patient_uses_bearer(monkeypatch, redox_env):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer tok-x"
        assert str(request.url).endswith("/Patient/abc-123")
        return httpx.Response(200, json={"resourceType": "Patient", "id": "abc-123"})

    _patch_httpx(monkeypatch, handler)
    pat = redox.fetch_patient(access_token="tok-x", patient_fhir_id="abc-123")
    assert pat["id"] == "abc-123"


def test_fetch_medications_uses_medication_request_resource(monkeypatch, redox_env):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"resourceType": "Bundle", "entry": []})

    _patch_httpx(monkeypatch, handler)
    redox.fetch_medications(access_token="tok-x", patient_fhir_id="p1")
    assert "/MedicationRequest?" in captured["url"]
    assert "MedicationStatement" not in captured["url"]


def test_destination_override_overrides_env(monkeypatch, redox_env):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"resourceType": "Patient", "id": "p"})

    _patch_httpx(monkeypatch, handler)
    redox.fetch_patient(
        access_token="tok-x",
        patient_fhir_id="p",
        destination_path="custom-org/Production",
    )
    assert "/custom-org/Production/Patient/p" in captured["url"]
    assert "redox-fhir-sandbox" not in captured["url"]


def test_write_service_request_returns_id_from_body(monkeypatch, redox_env):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        body = json.loads(request.content)
        assert body["resourceType"] == "ServiceRequest"
        assert body["intent"] == "referral"
        assert body["subject"]["reference"] == "Patient/p1"
        assert body["performerType"] == {"text": "Cardiology"}
        return httpx.Response(201, json={"id": "sr-99", "resourceType": "ServiceRequest"})

    _patch_httpx(monkeypatch, handler)
    rid = redox.write_service_request(
        access_token="tok-x",
        patient_fhir_id="p1",
        referral_id=42,
        specialty_desc="Cardiology",
        reason="chest pain",
        requesting_provider_name="Dr Smith",
    )
    assert rid == "sr-99"


def test_write_service_request_falls_back_to_location_header(monkeypatch, redox_env):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            headers={"Location": "https://api.redoxengine.com/.../ServiceRequest/sr-loc"},
            json={},
        )

    _patch_httpx(monkeypatch, handler)
    rid = redox.write_service_request(
        access_token="tok-x",
        patient_fhir_id="p1",
        referral_id=1,
        specialty_desc=None,
        reason=None,
        requesting_provider_name=None,
    )
    assert rid == "sr-loc"


def test_redact_strips_token_fields():
    payload = {
        "access_token": "secret",
        "client_assertion": "secret-jwt",
        "patient": {"id": "abc", "private_key": "leak"},
        "list": [{"refresh_token": "secret"}, {"id": "ok"}],
    }
    out = redox._redact(payload)
    assert out["access_token"] == "***"
    assert out["client_assertion"] == "***"
    assert out["patient"]["private_key"] == "***"
    assert out["patient"]["id"] == "abc"
    assert out["list"][0]["refresh_token"] == "***"
    assert out["list"][1]["id"] == "ok"


def test_redox_error_is_subclass_of_ehr_error():
    from docstats.ehr.registry import EHRError

    assert issubclass(RedoxError, EHRError)


def test_redox_registered_in_registry():
    from docstats.ehr import registry as _registry

    # Force import side-effect (already imported elsewhere, but defensive).
    from docstats.ehr import redox as _redox  # noqa: F401

    assert "redox" in _registry.list_vendors()
