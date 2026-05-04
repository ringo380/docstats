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
    fake_endpoints = epic.EpicEndpoints(
        authorize_endpoint="https://fake-epic.test/oauth2/authorize",
        token_endpoint="https://fake-epic.test/oauth2/token",
        fhir_base="https://fake-epic.test/api/FHIR/R4",
    )
    # Seed under both the env base (used by initial discover() before any
    # connection exists) AND the stored iss / fhir_base (used by post-connect
    # calls that pass iss_override=conn.iss for multi-tenant safety).
    epic._DISCOVERY_CACHE["https://fake-epic.test"] = (fake_endpoints, 9999999999.0)
    epic._DISCOVERY_CACHE["https://fake-epic.test/api/FHIR/R4"] = (fake_endpoints, 9999999999.0)
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


# ---------------------------------------------------------------------------
# EHR-launch route (/ehr/launch/epic)
# ---------------------------------------------------------------------------


def test_ehr_launch_valid_iss_redirects_to_epic(ehr_client, monkeypatch):
    """Valid iss in allowlist → session stores state + redirects to Epic authorize."""
    client, storage, _uid = ehr_client
    iss = "https://fake-epic.test"
    monkeypatch.setenv("EPIC_EHR_LAUNCH_ISS_ALLOWLIST", iss)
    # Pre-populate discovery cache for the iss so we don't make a real HTTP call.
    epic._DISCOVERY_CACHE[iss] = (
        epic.EpicEndpoints(
            authorize_endpoint="https://fake-epic.test/oauth2/authorize",
            token_endpoint="https://fake-epic.test/oauth2/token",
            fhir_base="https://fake-epic.test/api/FHIR/R4",
        ),
        9999999999.0,
    )
    resp = client.get(
        "/ehr/launch/epic",
        params={"iss": iss, "launch": "launch-token-abc"},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    location = resp.headers["location"]
    assert "fake-epic.test/oauth2/authorize" in location
    assert "launch=launch-token-abc" in location


def test_ehr_launch_iss_not_in_allowlist_returns_400(ehr_client, monkeypatch):
    client, _storage, _uid = ehr_client
    monkeypatch.setenv("EPIC_EHR_LAUNCH_ISS_ALLOWLIST", "https://other.test")
    resp = client.get(
        "/ehr/launch/epic",
        params={"iss": "https://attacker.test", "launch": "t"},
        follow_redirects=False,
    )
    assert resp.status_code == 400


def test_ehr_launch_empty_allowlist_returns_400(ehr_client, monkeypatch):
    client, _storage, _uid = ehr_client
    monkeypatch.delenv("EPIC_EHR_LAUNCH_ISS_ALLOWLIST", raising=False)
    resp = client.get(
        "/ehr/launch/epic",
        params={"iss": "https://fake-epic.test", "launch": "t"},
        follow_redirects=False,
    )
    assert resp.status_code == 400


def test_ehr_launch_missing_params_returns_400(ehr_client):
    """Missing iss or launch query params → 400."""
    client, _storage, _uid = ehr_client
    resp = client.get("/ehr/launch/epic", follow_redirects=False)
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# _maybe_refresh (token rotation)
# ---------------------------------------------------------------------------


def _make_connection(
    storage: Storage,
    user_id: int,
    *,
    expires_in_seconds: int = 3600,
    refresh_token: str | None = "RT",
) -> object:
    """Insert an EHRConnection row and return it."""
    from docstats.ehr.crypto import encrypt_token

    access_enc = encrypt_token("AT")
    refresh_enc = encrypt_token(refresh_token) if refresh_token else None
    expires_at = datetime.now(tz=timezone.utc) + timedelta(seconds=expires_in_seconds)
    storage.create_ehr_connection(
        user_id=user_id,
        ehr_vendor="epic_sandbox",
        iss="https://fake-epic.test",
        access_token_enc=access_enc,
        refresh_token_enc=refresh_enc,
        expires_at=expires_at,
        scope="openid",
        patient_fhir_id="PAT-99",
    )
    return storage.get_active_ehr_connection(user_id, "epic_sandbox")


def test_maybe_refresh_returns_existing_token_when_not_expiring(ehr_client):
    """Connection with plenty of time left → original token returned, no refresh call."""
    client, storage, user_id = ehr_client
    _make_connection(storage, user_id, expires_in_seconds=3600)
    conn = storage.get_active_ehr_connection(user_id, "epic_sandbox")
    assert conn is not None

    refresh_called = {"n": 0}

    def _fake_refresh(_rt):
        refresh_called["n"] += 1
        from docstats.ehr.epic import TokenResponse

        return TokenResponse(
            access_token="NEW_AT",
            refresh_token="NEW_RT",
            expires_in=3600,
            scope="openid",
            patient_fhir_id=None,
            id_token=None,
        )

    from docstats.routes import ehr as _ehr_mod

    import docstats.ehr.epic as _epic_mod

    _epic_mod_orig = _epic_mod.refresh
    _epic_mod.refresh = _fake_refresh
    try:
        token = _ehr_mod._maybe_refresh(conn, storage)
    finally:
        _epic_mod.refresh = _epic_mod_orig

    assert refresh_called["n"] == 0
    assert token == "AT"


def test_maybe_refresh_rotates_near_expiry(ehr_client):
    """Connection expiring within 60s → refresh called, new token returned."""
    client, storage, user_id = ehr_client
    _make_connection(storage, user_id, expires_in_seconds=30, refresh_token="OLD_RT")
    conn = storage.get_active_ehr_connection(user_id, "epic_sandbox")

    from docstats.ehr.epic import TokenResponse

    new_tok = TokenResponse(
        access_token="NEW_AT",
        refresh_token="NEW_RT",
        expires_in=3600,
        scope="openid",
        patient_fhir_id=None,
        id_token=None,
    )

    import docstats.ehr.epic as _epic_mod
    from docstats.routes import ehr as _ehr_mod

    _epic_mod_orig = _epic_mod.refresh
    _epic_mod.refresh = lambda _rt, *, iss_override=None: new_tok
    try:
        token = _ehr_mod._maybe_refresh(conn, storage)
    finally:
        _epic_mod.refresh = _epic_mod_orig

    assert token == "NEW_AT"
    # Verify connection row was updated.
    updated = storage.get_active_ehr_connection(user_id, "epic_sandbox")
    assert updated is not None


def test_maybe_refresh_failure_returns_stale_token_and_audits(ehr_client):
    """Refresh failure → stale token returned; audit ehr.token_refresh_failed."""
    client, storage, user_id = ehr_client
    _make_connection(storage, user_id, expires_in_seconds=10, refresh_token="RT")
    conn = storage.get_active_ehr_connection(user_id, "epic_sandbox")

    from docstats.ehr.epic import EpicError

    import docstats.ehr.epic as _epic_mod
    from docstats.routes import ehr as _ehr_mod

    def _fail_refresh(_rt, *, iss_override=None):
        raise EpicError("boom")

    _epic_mod_orig = _epic_mod.refresh
    _epic_mod.refresh = _fail_refresh
    try:
        token = _ehr_mod._maybe_refresh(conn, storage)
    finally:
        _epic_mod.refresh = _epic_mod_orig

    # Stale token returned (not None / exception).
    assert token == "AT"
    # Audit event recorded.
    events = storage.list_audit_events(actor_user_id=user_id, limit=10)
    actions = [e.action for e in events]
    assert "ehr.token_refresh_failed" in actions


# ---------------------------------------------------------------------------
# Cerner route tests (Phase 12.C)
# ---------------------------------------------------------------------------


@pytest.fixture
def cerner_env(monkeypatch, tmp_path):
    from docstats.ehr import cerner

    monkeypatch.setenv("EHR_CERNER_OAUTH_ENABLED", "1")
    monkeypatch.setenv("EHR_TOKEN_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("CERNER_CLIENT_ID", "cerner-cid")
    monkeypatch.setenv("CERNER_REDIRECT_URI", "https://referme.help/ehr/callback/cerner")
    monkeypatch.setenv("CERNER_SANDBOX_TENANT_ID", "ec2458f2-1e24-41c8-b71b-0e701af7583d")

    cerner._DISCOVERY_CACHE.clear()
    cerner._DISCOVERY_CACHE[
        "https://fhir-myrecord.cerner.com/r4/ec2458f2-1e24-41c8-b71b-0e701af7583d"
    ] = (
        cerner.CernerEndpoints(
            authorize_endpoint="https://fhir-myrecord.cerner.com/oauth2/authorize",
            token_endpoint="https://fhir-myrecord.cerner.com/oauth2/token",
            fhir_base="https://fhir-myrecord.cerner.com/r4/ec2458f2-1e24-41c8-b71b-0e701af7583d",
        ),
        9999999999.0,
    )
    yield
    cerner._DISCOVERY_CACHE.clear()


def _cerner_handler():
    """Default Cerner mock: token endpoint + Patient.read for PAT-C1."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth2/token"):
            return httpx.Response(
                200,
                json={
                    "access_token": "CAT",
                    "refresh_token": "CRT",
                    "expires_in": 3600,
                    "scope": "openid fhirUser",
                    "patient": "PAT-C1",
                },
            )
        if "/Patient/PAT-C1" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "resourceType": "Patient",
                    "id": "PAT-C1",
                    "name": [{"use": "official", "given": ["Alice"], "family": "Chen"}],
                    "birthDate": "1980-06-15",
                    "gender": "female",
                },
            )
        return httpx.Response(404)

    return handler


@pytest.fixture
def cerner_client(tmp_path: Path, cerner_env, monkeypatch):

    storage = Storage(db_path=tmp_path / "test.db")
    user_id = storage.create_user("c@example.com", "pw")
    storage.record_phi_consent(
        user_id=user_id,
        phi_consent_version=CURRENT_PHI_CONSENT_VERSION,
        ip_address="",
        user_agent="",
    )
    user = _fake_user(user_id, "c@example.com")
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_current_user] = lambda: user

    real_client = httpx.Client
    handler = _cerner_handler()
    monkeypatch.setattr(
        "docstats.ehr.cerner.httpx.Client",
        lambda *a, **kw: real_client(*a, transport=httpx.MockTransport(handler), **kw),
    )

    client = TestClient(app)
    yield client, storage, user_id
    app.dependency_overrides.clear()


def test_cerner_routes_404_when_disabled(tmp_path, monkeypatch):
    monkeypatch.delenv("EHR_CERNER_OAUTH_ENABLED", raising=False)
    storage = Storage(db_path=tmp_path / "test.db")
    user_id = storage.create_user("d@example.com", "pw")
    user = _fake_user(user_id, "d@example.com")
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_current_user] = lambda: user
    client = TestClient(app)
    resp = client.get("/ehr/connect/cerner", follow_redirects=False)
    assert resp.status_code == 404
    app.dependency_overrides.clear()


def test_cerner_connect_redirects_to_cerner(cerner_client):
    client, _, _ = cerner_client
    resp = client.get("/ehr/connect/cerner", follow_redirects=False)
    assert resp.status_code == 303
    loc = resp.headers["location"]
    assert loc.startswith("https://fhir-myrecord.cerner.com/oauth2/authorize?")
    assert "client_id=cerner-cid" in loc
    assert "code_challenge=" in loc
    assert "state=" in loc
    # Cerner patient-persona requires aud = fhir_base.
    assert "aud=https%3A%2F%2Ffhir-myrecord.cerner.com%2Fr4%2Fec2458f2" in loc


def test_cerner_callback_creates_connection(cerner_client):
    client, storage, user_id = cerner_client
    resp1 = client.get("/ehr/connect/cerner", follow_redirects=False)
    state = resp1.headers["location"].split("state=")[1].split("&")[0]
    resp2 = client.get(f"/ehr/callback/cerner?code=THE_CODE&state={state}", follow_redirects=False)
    assert resp2.status_code == 303
    assert resp2.headers["location"] == "/ehr/import/review"

    conn = storage.get_active_ehr_connection(user_id, "cerner_oauth")
    assert conn is not None
    assert conn.ehr_vendor == "cerner_oauth"
    assert conn.patient_fhir_id == "PAT-C1"
    assert conn.access_token_enc != "CAT"  # Fernet ciphertext, not plaintext

    events = storage.list_audit_events(actor_user_id=user_id, limit=10)
    assert any(e.action == "ehr.connected" for e in events)


def test_cerner_callback_state_mismatch_redirects(cerner_client):
    client, storage, user_id = cerner_client
    resp = client.get("/ehr/callback/cerner?code=x&state=bogus", follow_redirects=False)
    assert resp.status_code == 303
    assert "ehr_error=state_mismatch" in resp.headers["location"]
    events = storage.list_audit_events(actor_user_id=user_id, limit=10)
    assert any(e.action == "ehr.connect_failed" for e in events)


def test_cerner_disconnect_revokes_connection(cerner_client):
    client, storage, user_id = cerner_client
    storage.create_ehr_connection(
        user_id=user_id,
        ehr_vendor="cerner_oauth",
        iss="https://fhir-myrecord.cerner.com/r4/ec2458f2",
        access_token_enc="ENC",
        refresh_token_enc=None,
        expires_at=datetime.now(tz=timezone.utc) + timedelta(hours=1),
        scope="openid",
        patient_fhir_id="PAT-C1",
    )
    resp = client.post("/ehr/disconnect/cerner", follow_redirects=False)
    assert resp.status_code == 303
    assert storage.get_active_ehr_connection(user_id, "cerner_oauth") is None
    events = storage.list_audit_events(actor_user_id=user_id, limit=10)
    assert any(e.action == "ehr.disconnected" for e in events)


def test_maybe_refresh_dispatches_to_cerner(tmp_path, monkeypatch, cerner_env):
    """_maybe_refresh uses cerner.refresh when conn.ehr_vendor == 'cerner_oauth'."""
    from docstats.ehr import cerner as _cerner_mod
    from docstats.ehr.crypto import encrypt_token
    from docstats.routes import ehr as _ehr_mod

    storage = Storage(db_path=tmp_path / "test.db")
    user_id = storage.create_user("e@example.com", "pw")
    now = datetime.now(tz=timezone.utc)
    storage.create_ehr_connection(
        user_id=user_id,
        ehr_vendor="cerner_oauth",
        iss="https://fhir-myrecord.cerner.com/r4/ec2458f2",
        access_token_enc=encrypt_token("CAT"),
        refresh_token_enc=encrypt_token("CRT"),
        expires_at=now + timedelta(seconds=10),  # about to expire → triggers refresh
        scope="openid",
        patient_fhir_id="PAT-C1",
    )
    conn = storage.get_active_ehr_connection(user_id, "cerner_oauth")

    refreshed = []

    def _mock_refresh(rt: str, *, iss_override=None):
        refreshed.append(rt)
        return _cerner_mod.TokenResponse(
            access_token="NEW_CAT",
            refresh_token="NEW_CRT",
            expires_in=3600,
            scope="openid",
            patient_fhir_id=None,
            id_token=None,
        )

    orig = _cerner_mod.refresh
    _cerner_mod.refresh = _mock_refresh
    try:
        token = _ehr_mod._maybe_refresh(conn, storage)
    finally:
        _cerner_mod.refresh = orig

    assert token == "NEW_CAT"
    assert refreshed == ["CRT"]
