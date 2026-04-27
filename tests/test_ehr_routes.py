"""Route tests for /ehr/* — connect → callback → review → confirm flow."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
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


def _fake_user(uid: int, email: str, *, consent: bool = True):
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
        "phi_consent_at": "2026-01-01" if consent else None,
        "phi_consent_version": CURRENT_PHI_CONSENT_VERSION if consent else None,
        "phi_consent_ip": None,
        "phi_consent_user_agent": None,
        "active_org_id": None,
    }


def _patient_handler():
    """Default Epic mock: token endpoint + Patient.read for PAT-99."""

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

    return handler


@pytest.fixture
def epic_env(monkeypatch):
    monkeypatch.setenv("EHR_EPIC_SANDBOX_ENABLED", "1")
    monkeypatch.setenv("EHR_TOKEN_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("EPIC_CLIENT_ID", "fake-cid")
    monkeypatch.setenv("EPIC_CLIENT_SECRET", "fake-secret")
    monkeypatch.setenv("EPIC_REDIRECT_URI", "https://referme.help/ehr/callback/epic")
    monkeypatch.setenv("EPIC_SANDBOX_BASE_URL", "https://fake-epic.test")

    epic._DISCOVERY_CACHE.clear()
    epic._DISCOVERY_CACHE["https://fake-epic.test"] = (
        epic.EpicEndpoints(
            authorize_endpoint="https://fake-epic.test/oauth2/authorize",
            token_endpoint="https://fake-epic.test/oauth2/token",
            fhir_base="https://fake-epic.test/api/FHIR/R4",
        ),
        9999999999.0,
    )
    yield
    epic._DISCOVERY_CACHE.clear()


@pytest.fixture
def ehr_client(tmp_path: Path, epic_env, monkeypatch):
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

    # Default mock for Epic httpx calls.
    real_client = httpx.Client
    handler = _patient_handler()
    monkeypatch.setattr(
        "docstats.ehr.epic.httpx.Client",
        lambda *a, **kw: real_client(*a, transport=httpx.MockTransport(handler), **kw),
    )

    client = TestClient(app)
    yield client, storage, user_id
    app.dependency_overrides.clear()


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
    resp = client.get("/ehr/callback/epic?code=foo&state=bogus", follow_redirects=False)
    assert resp.status_code == 303
    assert "ehr_error=state_mismatch" in resp.headers["location"]
    events = storage.list_audit_events(actor_user_id=user_id, limit=10)
    assert any(e.action == "ehr.connect_failed" for e in events)


def test_callback_oauth_error_param_is_allowlisted(ehr_client):
    """Attacker-supplied error string must not pass through to /profile."""
    client, _, _ = ehr_client
    resp = client.get("/ehr/callback/epic?error=<script>alert(1)</script>", follow_redirects=False)
    assert resp.status_code == 303
    # We map upstream OAuth errors to the literal "oauth_error" reason.
    assert resp.headers["location"] == "/profile?ehr_error=oauth_error"


def test_callback_creates_connection_no_phi_in_session(ehr_client):
    """Callback persists connection + redirects to review. No PHI in cookie."""
    client, storage, user_id = ehr_client
    resp1 = client.get("/ehr/connect/epic", follow_redirects=False)
    state = resp1.headers["location"].split("state=")[1].split("&")[0]
    resp2 = client.get(f"/ehr/callback/epic?code=THE_CODE&state={state}", follow_redirects=False)
    assert resp2.status_code == 303
    assert resp2.headers["location"] == "/ehr/import/review"

    conn = storage.get_active_ehr_connection(user_id, "epic_sandbox")
    assert conn is not None
    assert conn.access_token_enc != "AT"  # Fernet ciphertext, not plaintext
    assert conn.patient_fhir_id == "PAT-99"
    assert conn.iss.rstrip("/") == conn.iss  # stored without trailing slash

    # Critical: nothing PHI-shaped in the session cookie. The cookie may
    # contain opaque session_id / oauth state, but no patient fields.
    set_cookie = resp2.headers.get("set-cookie", "")
    for needle in ("Sam", "Carter", "PAT-99", "MRN-7", "1975-01-02"):
        assert needle not in set_cookie, f"PHI {needle!r} leaked into Set-Cookie"

    events = storage.list_audit_events(actor_user_id=user_id, limit=10)
    assert any(e.action == "ehr.connected" for e in events)


def test_review_without_connection_redirects(ehr_client):
    client, _, _ = ehr_client
    resp = client.get("/ehr/import/review", follow_redirects=False)
    assert resp.status_code == 303
    assert "ehr_error=no_active_connection" in resp.headers["location"]


def test_review_refetches_patient_from_active_connection(ehr_client):
    client, _, _ = ehr_client
    # Run the full callback to seed an active connection.
    resp1 = client.get("/ehr/connect/epic", follow_redirects=False)
    state = resp1.headers["location"].split("state=")[1].split("&")[0]
    client.get(f"/ehr/callback/epic?code=C&state={state}", follow_redirects=False)

    resp = client.get("/ehr/import/review")
    assert resp.status_code == 200
    assert "Sam" in resp.text and "Carter" in resp.text
    assert "MRN-7" in resp.text


def test_disconnect_revokes_and_redirects_for_non_htmx(ehr_client):
    client, storage, user_id = ehr_client
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
    # Non-htmx POST → 303 to /profile.
    resp = client.post("/ehr/disconnect/epic", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/profile"
    assert storage.get_active_ehr_connection(user_id, "epic_sandbox") is None


def test_disconnect_returns_partial_for_htmx(ehr_client):
    client, storage, user_id = ehr_client
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
    resp = client.post("/ehr/disconnect/epic", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert "Connect Epic Sandbox" in resp.text
    events = storage.list_audit_events(actor_user_id=user_id, limit=10)
    assert any(e.action == "ehr.disconnected" for e in events)


def test_confirm_create_new_creates_patient(ehr_client):
    client, storage, user_id = ehr_client
    # Run the full connect+callback to seed an active connection.
    resp1 = client.get("/ehr/connect/epic", follow_redirects=False)
    state = resp1.headers["location"].split("state=")[1].split("&")[0]
    client.get(f"/ehr/callback/epic?code=C&state={state}", follow_redirects=False)

    resp = client.post(
        "/ehr/import/confirm",
        data={"action": "create_new"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/patients/")

    scope = Scope(user_id=user_id)
    patients = storage.list_patients(scope)
    assert any(p.first_name == "Sam" and p.last_name == "Carter" for p in patients)
    events = storage.list_audit_events(actor_user_id=user_id, limit=20)
    assert any(e.action == "patient.imported_from_ehr" for e in events)


def test_confirm_no_active_connection_redirects(ehr_client):
    client, _, _ = ehr_client
    resp = client.post(
        "/ehr/import/confirm",
        data={"action": "create_new"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "ehr_error=no_active_connection" in resp.headers["location"]


def test_review_requires_phi_consent(tmp_path, epic_env, monkeypatch):
    """A user without PHI consent gets bounced from import_review."""
    storage = Storage(db_path=tmp_path / "test.db")
    user_id = storage.create_user("a@example.com", "pw")
    # Note: no record_phi_consent call.
    user = _fake_user(user_id, "a@example.com", consent=False)
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        client = TestClient(app)
        resp = client.get("/ehr/import/review", follow_redirects=False)
        # require_phi_consent raises PhiConsentRequiredException → 303 to consent flow.
        assert resp.status_code == 303
    finally:
        app.dependency_overrides.clear()
