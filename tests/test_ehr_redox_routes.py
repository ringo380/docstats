"""Route tests for /ehr/redox/* (Phase 12.E)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from docstats.auth import get_current_user
from docstats.ehr import redox
from docstats.storage import Storage, get_storage
from docstats.web import app


def _fake_user(
    user_id: int,
    email: str,
    *,
    active_org_id: int | None = None,
    is_org_admin: bool = False,
) -> dict:
    return {
        "id": user_id,
        "email": email,
        "display_name": None,
        "first_name": None,
        "last_name": None,
        "github_id": None,
        "github_login": None,
        "password_hash": "hashed",
        "created_at": "2026-01-01",
        "last_login_at": None,
        "terms_accepted_at": "2026-01-01",
        "active_org_id": active_org_id,
        "is_org_admin": is_org_admin,
    }


@pytest.fixture
def test_keypair() -> tuple[str, str]:
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
def reset_redox_cache():
    redox.reset_token_cache()
    yield
    redox.reset_token_cache()


@pytest.fixture
def redox_env(monkeypatch, test_keypair):
    private_pem, _ = test_keypair
    monkeypatch.setenv("EHR_REDOX_ENABLED", "1")
    monkeypatch.setenv("REDOX_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("REDOX_KEY_ID", "test-kid")
    monkeypatch.setenv("REDOX_PRIVATE_KEY_PEM", private_pem)
    monkeypatch.delenv("REDOX_TOKEN_URL", raising=False)
    monkeypatch.delenv("REDOX_FHIR_BASE", raising=False)


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    return Storage(db_path=tmp_path / "redox.db")


@pytest.fixture
def org_admin(storage: Storage):
    user_id = storage.create_user("admin@example.com", "hashed")
    org = storage.create_organization(name="Acme Clinic", slug="acme")
    storage.create_membership(organization_id=org.id, user_id=user_id, role="admin")
    storage.set_active_org(user_id, org.id)
    user = _fake_user(user_id, "admin@example.com", active_org_id=org.id, is_org_admin=True)
    return user_id, org, user


def _client_with(storage: Storage, user: dict | None) -> TestClient:
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


def _cleanup() -> None:
    app.dependency_overrides.clear()


def _patch_token_endpoint(monkeypatch, status: int = 200, json_body: dict | None = None):
    body = json_body or {"access_token": "tok-mock", "expires_in": 300}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body)

    real_client = httpx.Client
    monkeypatch.setattr(
        "docstats.ehr.redox.httpx.Client",
        lambda *a, **kw: real_client(*a, transport=httpx.MockTransport(handler), **kw),
    )


def _patch_redox_api(monkeypatch, handler):
    """Patch httpx for Redox calls with a caller-supplied request handler.

    The handler receives every httpx request (token mint + FHIR) and decides
    what to return. Use ``request.url.path`` to dispatch.
    """
    real_client = httpx.Client
    monkeypatch.setattr(
        "docstats.ehr.redox.httpx.Client",
        lambda *a, **kw: real_client(*a, transport=httpx.MockTransport(handler), **kw),
    )


def _seed_org_with_redox(storage: Storage, slug: str = "acme"):
    """Seed an org + admin + active Redox connection for import-flow tests."""
    user_id = storage.create_user("clinician@example.com", "hashed")
    org = storage.create_organization(name="Acme", slug=slug)
    storage.create_membership(organization_id=org.id, user_id=user_id, role="admin")
    storage.set_active_org(user_id, org.id)
    # PHI consent — required for import routes.
    from docstats.phi import CURRENT_PHI_CONSENT_VERSION

    storage.record_phi_consent(
        user_id=user_id,
        phi_consent_version=CURRENT_PHI_CONSENT_VERSION,
        ip_address="",
        user_agent="",
    )
    storage.create_org_ehr_connection(
        organization_id=org.id,
        ehr_vendor="redox",
        iss="redox-fhir-sandbox/Development",
        scope="system/Patient.read",
    )
    user = _fake_user(user_id, "clinician@example.com", active_org_id=org.id, is_org_admin=True)
    user["phi_consent_version"] = CURRENT_PHI_CONSENT_VERSION
    user["phi_consent_at"] = "2026-01-01"
    return user_id, org, user


# ---------------------------------------------------------------------------
# Feature flag gating
# ---------------------------------------------------------------------------


def test_connect_form_404_when_flag_unset(storage, org_admin, monkeypatch):
    monkeypatch.delenv("EHR_REDOX_ENABLED", raising=False)
    _, _, user = org_admin
    try:
        resp = _client_with(storage, user).get("/ehr/redox/connect")
        assert resp.status_code == 404
    finally:
        _cleanup()


def test_connect_post_404_when_flag_unset(storage, org_admin, monkeypatch):
    monkeypatch.delenv("EHR_REDOX_ENABLED", raising=False)
    _, _, user = org_admin
    try:
        resp = _client_with(storage, user).post(
            "/ehr/redox/connect", data={"destination_path": "x/Dev"}
        )
        assert resp.status_code == 404
    finally:
        _cleanup()


# ---------------------------------------------------------------------------
# Admin gating
# ---------------------------------------------------------------------------


def test_solo_user_gets_403(storage, redox_env):
    user_id = storage.create_user("solo@example.com", "hashed")
    user = _fake_user(user_id, "solo@example.com", active_org_id=None)
    try:
        resp = _client_with(storage, user).get("/ehr/redox/connect")
        assert resp.status_code == 403
    finally:
        _cleanup()


@pytest.mark.parametrize("role", ["read_only", "staff", "clinician", "coordinator"])
def test_sub_admin_gets_403(storage, redox_env, role):
    user_id = storage.create_user(f"{role}@example.com", "hashed")
    org = storage.create_organization(name="X", slug=f"x-{role}")
    storage.create_membership(organization_id=org.id, user_id=user_id, role=role)
    storage.set_active_org(user_id, org.id)
    user = _fake_user(user_id, f"{role}@example.com", active_org_id=org.id, is_org_admin=False)
    try:
        resp = _client_with(storage, user).get("/ehr/redox/connect")
        assert resp.status_code == 403
    finally:
        _cleanup()


# ---------------------------------------------------------------------------
# Connect flow
# ---------------------------------------------------------------------------


def test_admin_get_connect_form_200(storage, org_admin, redox_env):
    _, _, user = org_admin
    try:
        resp = _client_with(storage, user).get("/ehr/redox/connect")
        assert resp.status_code == 200
        assert b"Connect Redox" in resp.content
        assert b"redox-fhir-sandbox/Development" in resp.content
    finally:
        _cleanup()


def test_connect_creates_org_scoped_row(storage, org_admin, redox_env, monkeypatch):
    _patch_token_endpoint(monkeypatch)
    _, org, user = org_admin
    try:
        client = _client_with(storage, user)
        resp = client.post(
            "/ehr/redox/connect",
            data={"destination_path": "  acme-prod/Production  "},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "ehr_connected=redox" in resp.headers["location"]

        active = storage.get_active_org_ehr_connection(org.id, "redox")
        assert active is not None
        assert active.user_id is None
        assert active.organization_id == org.id
        # Whitespace + leading/trailing slashes stripped.
        assert active.iss == "acme-prod/Production"
    finally:
        _cleanup()


def test_connect_validates_creds_via_token_mint(storage, org_admin, redox_env, monkeypatch):
    """Token endpoint failure → 303 redirect with token_exchange error, no row created."""
    _patch_token_endpoint(monkeypatch, status=400, json_body={"error": "invalid_request"})
    _, org, user = org_admin
    try:
        resp = _client_with(storage, user).post(
            "/ehr/redox/connect",
            data={"destination_path": "x/Dev"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "error=token_exchange" in resp.headers["location"]
        # No row inserted.
        assert storage.get_active_org_ehr_connection(org.id, "redox") is None
    finally:
        _cleanup()


def test_connect_missing_destination_path_redirects(storage, org_admin, redox_env, monkeypatch):
    _patch_token_endpoint(monkeypatch)
    _, org, user = org_admin
    try:
        resp = _client_with(storage, user).post(
            "/ehr/redox/connect",
            data={"destination_path": "   "},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "error=missing_destination" in resp.headers["location"]
        assert storage.get_active_org_ehr_connection(org.id, "redox") is None
    finally:
        _cleanup()


def test_connect_handles_missing_env_config(storage, org_admin, monkeypatch):
    monkeypatch.setenv("EHR_REDOX_ENABLED", "1")
    monkeypatch.delenv("REDOX_CLIENT_ID", raising=False)
    _, org, user = org_admin
    try:
        resp = _client_with(storage, user).post(
            "/ehr/redox/connect",
            data={"destination_path": "x/Dev"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "error=server_config" in resp.headers["location"]
        assert storage.get_active_org_ehr_connection(org.id, "redox") is None
    finally:
        _cleanup()


def test_connect_replaces_prior_active_row(storage, org_admin, redox_env, monkeypatch):
    """Re-connecting revokes the prior org row and inserts a new one."""
    _patch_token_endpoint(monkeypatch)
    _, org, user = org_admin
    try:
        client = _client_with(storage, user)
        client.post(
            "/ehr/redox/connect",
            data={"destination_path": "first/Development"},
            follow_redirects=False,
        )
        client.post(
            "/ehr/redox/connect",
            data={"destination_path": "second/Production"},
            follow_redirects=False,
        )
        active = storage.get_active_org_ehr_connection(org.id, "redox")
        assert active is not None
        assert active.iss == "second/Production"
        # First row should be revoked.
        rows = storage._conn.execute(
            "SELECT iss, revoked_at FROM ehr_connections WHERE organization_id = ? ORDER BY id",
            (org.id,),
        ).fetchall()
        assert len(rows) == 2
        assert rows[0]["revoked_at"] is not None
        assert rows[1]["revoked_at"] is None
    finally:
        _cleanup()


# ---------------------------------------------------------------------------
# Disconnect flow
# ---------------------------------------------------------------------------


def test_disconnect_revokes_active(storage, org_admin, redox_env, monkeypatch):
    _patch_token_endpoint(monkeypatch)
    _, org, user = org_admin
    try:
        client = _client_with(storage, user)
        client.post(
            "/ehr/redox/connect",
            data={"destination_path": "x/Dev"},
            follow_redirects=False,
        )
        resp = client.post("/ehr/redox/disconnect", follow_redirects=False)
        assert resp.status_code == 303
        assert "ehr_disconnected=redox" in resp.headers["location"]
        assert storage.get_active_org_ehr_connection(org.id, "redox") is None
    finally:
        _cleanup()


def test_disconnect_idempotent(storage, org_admin, redox_env):
    """Disconnect with no active connection is a no-op (303 redirect, no audit)."""
    _, org, user = org_admin
    try:
        resp = _client_with(storage, user).post("/ehr/redox/disconnect", follow_redirects=False)
        assert resp.status_code == 303
        assert storage.get_active_org_ehr_connection(org.id, "redox") is None
    finally:
        _cleanup()


def test_disconnect_404_when_disabled(storage, org_admin, monkeypatch):
    monkeypatch.delenv("EHR_REDOX_ENABLED", raising=False)
    _, _, user = org_admin
    try:
        resp = _client_with(storage, user).post("/ehr/redox/disconnect")
        assert resp.status_code == 404
    finally:
        _cleanup()


# ---------------------------------------------------------------------------
# Patient import flow
# ---------------------------------------------------------------------------


_TEST_FHIR_ID = "patient-fhir-99"
_TEST_MRN = "MRN-42"
_TEST_MRN_SYSTEM = "http://hospital.smarthealthit.org"


def _patient_resource(
    fhir_id: str = _TEST_FHIR_ID,
    family: str = "Robel",
    given: str = "Alexander",
    dob: str = "2007-12-14",
):
    return {
        "resourceType": "Patient",
        "id": fhir_id,
        "name": [{"use": "official", "family": family, "given": [given]}],
        "birthDate": dob,
        "gender": "male",
        "identifier": [
            {
                "type": {"coding": [{"code": "MR"}]},
                "system": _TEST_MRN_SYSTEM,
                "value": _TEST_MRN,
            }
        ],
    }


def _import_flow_handler(*, search_hits: int = 1, fhir_id: str = _TEST_FHIR_ID):
    """Build an httpx handler that supports token + Patient search + Patient read."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/v2/auth/token"):
            return httpx.Response(200, json={"access_token": "tok-mock", "expires_in": 300})
        if "/Patient" in path and request.url.query and b"identifier" in request.url.query:
            entries = [{"resource": _patient_resource(fhir_id=fhir_id)}] * search_hits
            return httpx.Response(200, json={"resourceType": "Bundle", "entry": entries})
        if path.endswith(f"/Patient/{fhir_id}"):
            return httpx.Response(200, json=_patient_resource(fhir_id=fhir_id))
        return httpx.Response(404, json={"error": "unmatched test handler"})

    return handler


def test_import_form_404_when_flag_unset(storage, monkeypatch):
    monkeypatch.delenv("EHR_REDOX_ENABLED", raising=False)
    _, _, user = _seed_org_with_redox(storage)
    try:
        resp = _client_with(storage, user).get("/ehr/redox/import")
        assert resp.status_code == 404
    finally:
        _cleanup()


def test_import_form_403_when_no_org(storage, redox_env):
    """Solo users get a 403 because Redox import is org-scoped."""
    user_id = storage.create_user("solo@example.com", "hashed")
    from docstats.phi import CURRENT_PHI_CONSENT_VERSION

    storage.record_phi_consent(
        user_id=user_id,
        phi_consent_version=CURRENT_PHI_CONSENT_VERSION,
        ip_address="",
        user_agent="",
    )
    user = _fake_user(user_id, "solo@example.com", active_org_id=None)
    user["phi_consent_version"] = CURRENT_PHI_CONSENT_VERSION
    user["phi_consent_at"] = "2026-01-01"
    try:
        resp = _client_with(storage, user).get("/ehr/redox/import")
        assert resp.status_code == 403
    finally:
        _cleanup()


def test_import_form_403_when_org_has_no_connection(storage, redox_env):
    """Org without an active Redox connection sees 403, not the form."""
    user_id = storage.create_user("admin@example.com", "hashed")
    org = storage.create_organization(name="No-Redox Co", slug="no-redox")
    storage.create_membership(organization_id=org.id, user_id=user_id, role="admin")
    storage.set_active_org(user_id, org.id)
    from docstats.phi import CURRENT_PHI_CONSENT_VERSION

    storage.record_phi_consent(
        user_id=user_id,
        phi_consent_version=CURRENT_PHI_CONSENT_VERSION,
        ip_address="",
        user_agent="",
    )
    user = _fake_user(user_id, "admin@example.com", active_org_id=org.id, is_org_admin=True)
    user["phi_consent_version"] = CURRENT_PHI_CONSENT_VERSION
    user["phi_consent_at"] = "2026-01-01"
    try:
        resp = _client_with(storage, user).get("/ehr/redox/import")
        assert resp.status_code == 403
    finally:
        _cleanup()


def test_import_form_renders_for_org_with_connection(storage, redox_env):
    _, _, user = _seed_org_with_redox(storage)
    try:
        resp = _client_with(storage, user).get("/ehr/redox/import")
        assert resp.status_code == 200
        assert b"Import Patient from Redox" in resp.content
        assert b"redox-fhir-sandbox/Development" in resp.content
    finally:
        _cleanup()


def test_import_lookup_redirects_to_review_on_hit(storage, redox_env, monkeypatch):
    _, _, user = _seed_org_with_redox(storage)
    _patch_redox_api(monkeypatch, _import_flow_handler())
    try:
        client = _client_with(storage, user)
        resp = client.post(
            "/ehr/redox/import/lookup",
            data={"mrn": _TEST_MRN, "mrn_system": _TEST_MRN_SYSTEM},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert f"/ehr/redox/import/review?fhir_id={_TEST_FHIR_ID}" in resp.headers["location"]
    finally:
        _cleanup()


def test_import_lookup_redirects_with_not_found_on_miss(storage, redox_env, monkeypatch):
    _, _, user = _seed_org_with_redox(storage)
    _patch_redox_api(monkeypatch, _import_flow_handler(search_hits=0))
    try:
        resp = _client_with(storage, user).post(
            "/ehr/redox/import/lookup",
            data={"mrn": "missing-mrn"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "error=patient_not_found" in resp.headers["location"]
    finally:
        _cleanup()


def test_import_lookup_ambiguous_mrn(storage, redox_env, monkeypatch):
    _, _, user = _seed_org_with_redox(storage)
    _patch_redox_api(monkeypatch, _import_flow_handler(search_hits=2))
    try:
        resp = _client_with(storage, user).post(
            "/ehr/redox/import/lookup",
            data={"mrn": _TEST_MRN},  # no mrn_system → ambiguous
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "error=ambiguous_mrn" in resp.headers["location"]
    finally:
        _cleanup()


def test_import_lookup_missing_mrn(storage, redox_env, monkeypatch):
    _, _, user = _seed_org_with_redox(storage)
    _patch_redox_api(monkeypatch, _import_flow_handler())
    try:
        resp = _client_with(storage, user).post(
            "/ehr/redox/import/lookup",
            data={"mrn": "   "},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "error=missing_mrn" in resp.headers["location"]
    finally:
        _cleanup()


def test_import_review_renders_with_patient(storage, redox_env, monkeypatch):
    _, _, user = _seed_org_with_redox(storage)
    _patch_redox_api(monkeypatch, _import_flow_handler())
    try:
        resp = _client_with(storage, user).get(f"/ehr/redox/import/review?fhir_id={_TEST_FHIR_ID}")
        assert resp.status_code == 200
        assert b"Robel" in resp.content
        assert b"Alexander" in resp.content
        assert b"2007-12-14" in resp.content
        # Hidden field carries fhir_id forward into confirm form.
        assert _TEST_FHIR_ID.encode() in resp.content
    finally:
        _cleanup()


def test_import_confirm_create_new_creates_patient(storage, redox_env, monkeypatch):
    _, org, user = _seed_org_with_redox(storage)
    _patch_redox_api(monkeypatch, _import_flow_handler())
    try:
        resp = _client_with(storage, user).post(
            "/ehr/redox/import/confirm",
            data={"action": "create_new", "fhir_id": _TEST_FHIR_ID},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/patients/")
        # Find the created patient in storage.
        from docstats.scope import Scope

        org_scope = Scope(user_id=None, organization_id=org.id, membership_role="admin")
        patients = storage.list_patients(org_scope, mrn=_TEST_MRN, limit=5)
        assert len(patients) == 1
        p = patients[0]
        assert p.first_name == "Alexander"
        assert p.last_name == "Robel"
        assert p.ehr_fhir_id == _TEST_FHIR_ID
    finally:
        _cleanup()


def test_import_confirm_merge_fills_only_blank_fields(storage, redox_env, monkeypatch):
    user_id, org, user = _seed_org_with_redox(storage)
    _patch_redox_api(monkeypatch, _import_flow_handler())
    from docstats.scope import Scope

    org_scope = Scope(user_id=None, organization_id=org.id, membership_role="admin")
    # Pre-existing patient with curated last_name + DOB; missing MRN.
    existing = storage.create_patient(
        org_scope,
        first_name="Alex",
        last_name="Robel",
        date_of_birth="2007-12-14",
        created_by_user_id=user_id,
    )
    try:
        resp = _client_with(storage, user).post(
            "/ehr/redox/import/confirm",
            data={
                "action": "merge",
                "fhir_id": _TEST_FHIR_ID,
                "patient_id": str(existing.id),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        # Re-fetch the existing patient and confirm MRN was filled in.
        merged = storage.get_patient(org_scope, existing.id)
        assert merged is not None
        assert merged.mrn == _TEST_MRN
        assert merged.ehr_fhir_id == _TEST_FHIR_ID
        # Curated first_name was NOT overwritten.
        assert merged.first_name == "Alex"
    finally:
        _cleanup()


def test_import_confirm_merge_requires_patient_id(storage, redox_env, monkeypatch):
    _, _, user = _seed_org_with_redox(storage)
    _patch_redox_api(monkeypatch, _import_flow_handler())
    try:
        resp = _client_with(storage, user).post(
            "/ehr/redox/import/confirm",
            data={"action": "merge", "fhir_id": _TEST_FHIR_ID},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "error=merge_requires_patient_id" in resp.headers["location"]
    finally:
        _cleanup()


def test_import_routes_404_when_flag_unset(storage, monkeypatch):
    monkeypatch.delenv("EHR_REDOX_ENABLED", raising=False)
    _, _, user = _seed_org_with_redox(storage)
    try:
        client = _client_with(storage, user)
        for path in (
            "/ehr/redox/import",
            f"/ehr/redox/import/review?fhir_id={_TEST_FHIR_ID}",
        ):
            assert client.get(path).status_code == 404
        assert client.post("/ehr/redox/import/lookup", data={"mrn": "x"}).status_code == 404
        assert (
            client.post(
                "/ehr/redox/import/confirm",
                data={"action": "create_new", "fhir_id": _TEST_FHIR_ID},
            ).status_code
            == 404
        )
    finally:
        _cleanup()
