"""Route tests for /ehr/* — connect → callback → review → confirm flow."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from docstats.auth import get_current_user
from docstats.ehr import epic
from docstats.phi import CURRENT_PHI_CONSENT_VERSION
from docstats.scope import Scope
from docstats.storage import Storage, get_storage
from docstats.web import app


def _fake_user(uid: int, email: str):
    return {
        "id": uid,
        "email": email,
        "display_name": None,
        "first_name": None,
        "last_name": None,
        "github_id": None,
        "github_login": None,
        "password_hash": "x",
        "created_at": "2026-01-01",
        "last_login_at": None,
        "terms_accepted_at": "2026-01-01",
        "phi_consent_at": "2026-01-01",
        "phi_consent_version": CURRENT_PHI_CONSENT_VERSION,
        "phi_consent_ip": None,
        "phi_consent_user_agent": None,
        "active_org_id": None,
    }


@pytest.fixture
def ehr_client(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("EHR_EPIC_SANDBOX_ENABLED", "1")
    monkeypatch.setenv("EHR_TOKEN_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("EPIC_CLIENT_ID", "fake-cid")
    monkeypatch.setenv("EPIC_CLIENT_SECRET", "fake-secret")
    monkeypatch.setenv("EPIC_REDIRECT_URI", "https://referme.help/ehr/callback/epic")
    monkeypatch.setenv("EPIC_SANDBOX_BASE_URL", "https://fake-epic.test")

    epic._DISCOVERY_CACHE.clear()
    # Pre-seed discovery cache to avoid mocking the well-known endpoint.
    epic._DISCOVERY_CACHE["https://fake-epic.test"] = (
        epic.EpicEndpoints(
            authorize_endpoint="https://fake-epic.test/oauth2/authorize",
            token_endpoint="https://fake-epic.test/oauth2/token",
            fhir_base="https://fake-epic.test/api/FHIR/R4",
        ),
        9999999999.0,
    )

    storage = Storage(db_path=tmp_path / "test.db")
    user_id = storage.create_user("a@example.com", "pw")
    storage.record_phi_consent(
        user_id=user_id,
        phi_consent_version=CURRENT_PHI_CONSENT_VERSION,
        ip_address="",
        user_agent="",
    )
    user = _fake_user(user_id, "a@example.com")
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_current_user] = lambda: user

    client = TestClient(app)
    yield client, storage, user_id
    app.dependency_overrides.clear()
    epic._DISCOVERY_CACHE.clear()


def test_routes_404_when_disabled(tmp_path, monkeypatch):
    monkeypatch.delenv("EHR_EPIC_SANDBOX_ENABLED", raising=False)
    storage = Storage(db_path=tmp_path / "test.db")
    user_id = storage.create_user("a@example.com", "pw")
    user = _fake_user(user_id, "a@example.com")
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_current_user] = lambda: user
    client = TestClient(app)
    resp = client.get("/ehr/connect/epic", follow_redirects=False)
    assert resp.status_code == 404
    app.dependency_overrides.clear()


def test_connect_redirects_to_epic(ehr_client):
    client, _, _ = ehr_client
    resp = client.get("/ehr/connect/epic", follow_redirects=False)
    assert resp.status_code == 303
    loc = resp.headers["location"]
    assert loc.startswith("https://fake-epic.test/oauth2/authorize?")
    assert "client_id=fake-cid" in loc
    assert "code_challenge=" in loc
    assert "state=" in loc


def test_callback_state_mismatch_audits_and_redirects(ehr_client):
    client, storage, user_id = ehr_client
    # No prior /connect call → no state in session
    resp = client.get("/ehr/callback/epic?code=foo&state=bogus", follow_redirects=False)
    assert resp.status_code == 303
    assert "ehr_error=state_mismatch" in resp.headers["location"]
    events = storage.list_audit_events(actor_user_id=user_id, limit=10)
    assert any(e.action == "ehr.connect_failed" for e in events)


def test_callback_full_flow_creates_connection_and_redirects_to_review(ehr_client, monkeypatch):
    client, storage, user_id = ehr_client

    # 1) Initiate connect to seed session state + verifier
    resp1 = client.get("/ehr/connect/epic", follow_redirects=False)
    assert resp1.status_code == 303
    loc = resp1.headers["location"]
    state = loc.split("state=")[1].split("&")[0]

    # 2) Mock Epic token + Patient.read endpoints
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth2/token"):
            return httpx.Response(
                200,
                json={
                    "access_token": "AT",
                    "refresh_token": "RT",
                    "expires_in": 3600,
                    "scope": "openid fhirUser",
                    "patient": "PAT-99",
                    "id_token": "IT",
                },
            )
        if "/Patient/PAT-99" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "resourceType": "Patient",
                    "id": "PAT-99",
                    "name": [{"use": "official", "given": ["Sam"], "family": "Carter"}],
                    "birthDate": "1975-01-02",
                    "gender": "male",
                    "identifier": [{"type": {"coding": [{"code": "MR"}]}, "value": "MRN-7"}],
                },
            )
        return httpx.Response(404)

    real_client = httpx.Client
    monkeypatch.setattr(
        "docstats.ehr.epic.httpx.Client",
        lambda *a, **kw: real_client(*a, transport=httpx.MockTransport(handler), **kw),
    )

    resp2 = client.get(f"/ehr/callback/epic?code=THE_CODE&state={state}", follow_redirects=False)
    assert resp2.status_code == 303
    assert resp2.headers["location"] == "/ehr/import/review"

    # Connection persisted with encrypted tokens (not plaintext).
    conn = storage.get_active_ehr_connection(user_id, "epic_sandbox")
    assert conn is not None
    assert conn.access_token_enc != "AT"
    assert conn.refresh_token_enc and conn.refresh_token_enc != "RT"
    assert conn.patient_fhir_id == "PAT-99"

    # ehr.connected audit event recorded.
    events = storage.list_audit_events(actor_user_id=user_id, limit=10)
    assert any(e.action == "ehr.connected" for e in events)


def test_review_without_pending_redirects(ehr_client):
    client, _, _ = ehr_client
    resp = client.get("/ehr/import/review", follow_redirects=False)
    assert resp.status_code == 303
    assert "ehr_error=no_pending_import" in resp.headers["location"]


def test_disconnect_revokes_and_audits(ehr_client):
    client, storage, user_id = ehr_client
    # Seed an active connection directly.
    from datetime import datetime, timedelta, timezone

    storage.create_ehr_connection(
        user_id=user_id,
        ehr_vendor="epic_sandbox",
        iss="x",
        access_token_enc="AT",
        refresh_token_enc=None,
        expires_at=datetime.now(tz=timezone.utc) + timedelta(hours=1),
        scope="openid",
        patient_fhir_id="P",
    )
    resp = client.post("/ehr/disconnect/epic")
    assert resp.status_code == 200
    assert "Connect Epic Sandbox" in resp.text
    assert storage.get_active_ehr_connection(user_id, "epic_sandbox") is None
    events = storage.list_audit_events(actor_user_id=user_id, limit=10)
    assert any(e.action == "ehr.disconnected" for e in events)


def test_confirm_create_new_creates_patient(ehr_client):
    client, storage, user_id = ehr_client
    # Stash a pending import in the session by hitting connect+callback or
    # by injecting via a session-modifying call. Easiest: call the review
    # template only after we drop a pending dict via an internal path. Since
    # TestClient sessions are server-controlled, set via a one-off route
    # call: we use the standard happy-path callback with mocked httpx.

    # Initiate connect for state.
    resp1 = client.get("/ehr/connect/epic", follow_redirects=False)
    state = resp1.headers["location"].split("state=")[1].split("&")[0]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth2/token"):
            return httpx.Response(
                200,
                json={
                    "access_token": "AT",
                    "expires_in": 3600,
                    "patient": "PAT-9",
                    "scope": "x",
                },
            )
        if "/Patient/PAT-9" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "resourceType": "Patient",
                    "id": "PAT-9",
                    "name": [{"use": "official", "given": ["Test"], "family": "Patient"}],
                    "birthDate": "1990-01-01",
                    "identifier": [{"value": "MRN-X"}],
                },
            )
        return httpx.Response(404)

    import docstats.ehr.epic as ep_module

    orig = ep_module.httpx.Client
    ep_module.httpx.Client = lambda *a, **kw: orig(  # type: ignore[assignment]
        *a, transport=httpx.MockTransport(handler), **kw
    )
    try:
        client.get(f"/ehr/callback/epic?code=C&state={state}", follow_redirects=False)
    finally:
        ep_module.httpx.Client = orig  # type: ignore[assignment]

    # Now confirm create_new
    resp = client.post(
        "/ehr/import/confirm",
        data={"action": "create_new"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/patients/")

    # Patient row exists in solo scope.
    scope = Scope(user_id=user_id)
    patients = storage.list_patients(scope)
    assert any(p.first_name == "Test" and p.last_name == "Patient" for p in patients)
    events = storage.list_audit_events(actor_user_id=user_id, limit=20)
    assert any(e.action == "patient.imported_from_ehr" for e in events)
