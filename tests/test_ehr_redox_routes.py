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
    user = _fake_user(
        user_id, "admin@example.com", active_org_id=org.id, is_org_admin=True
    )
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
        lambda *a, **kw: real_client(
            *a, transport=httpx.MockTransport(handler), **kw
        ),
    )


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
    user = _fake_user(
        user_id, f"{role}@example.com", active_org_id=org.id, is_org_admin=False
    )
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
            "SELECT iss, revoked_at FROM ehr_connections "
            "WHERE organization_id = ? ORDER BY id",
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
        resp = _client_with(storage, user).post(
            "/ehr/redox/disconnect", follow_redirects=False
        )
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
