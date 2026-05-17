"""Route tests for /ehr/connect/epic/patient/{patient_id} (Issue #155)."""

from __future__ import annotations


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

from tests.test_ehr_routes import _fake_user, _patient_handler


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
    epic._DISCOVERY_CACHE["https://fake-epic.test"] = (fake_endpoints, 9999999999.0)
    epic._DISCOVERY_CACHE["https://fake-epic.test/api/FHIR/R4"] = (
        fake_endpoints,
        9999999999.0,
    )
    yield
    epic._DISCOVERY_CACHE.clear()


def _bootstrap(tmp_path, monkeypatch, *, email="parent@example.com"):
    storage = Storage(db_path=tmp_path / "test.db")
    user_id = storage.create_user(email, "pw")
    storage.record_phi_consent(
        user_id=user_id,
        phi_consent_version=CURRENT_PHI_CONSENT_VERSION,
        ip_address="",
        user_agent="",
    )
    # Make the parent a dependent's manager — create a child patient.
    patient = storage.create_patient(
        Scope(user_id=user_id),
        first_name="Kid",
        last_name="Doe",
        relationship="child",
        ehr_fhir_id="PAT-99",
    )
    real_client = httpx.Client
    handler = _patient_handler()
    monkeypatch.setattr(
        "docstats.ehr.epic.httpx.Client",
        lambda *a, **kw: real_client(*a, transport=httpx.MockTransport(handler), **kw),
    )
    return storage, user_id, patient.id


def test_connect_patient_404_when_vendor_disabled(tmp_path, monkeypatch):
    monkeypatch.delenv("EHR_EPIC_SANDBOX_ENABLED", raising=False)
    storage, user_id, patient_id = _bootstrap(tmp_path, monkeypatch)
    user = _fake_user(user_id, "parent@example.com")
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        client = TestClient(app)
        resp = client.post(f"/ehr/connect/epic/patient/{patient_id}", follow_redirects=False)
        # _connect_flow's _require_vendor_enabled fires inside the route after
        # the cross-tenant guard; the cross-tenant guard succeeds (patient
        # exists in scope) so we get the vendor 404 from _connect_flow.
        assert resp.status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_connect_patient_cross_tenant_returns_404(tmp_path, monkeypatch, epic_env):
    """Parent A cannot launch MyChart on behalf of parent B's dependent."""
    storage = Storage(db_path=tmp_path / "test.db")
    a_id = storage.create_user("a@example.com", "pw")
    b_id = storage.create_user("b@example.com", "pw")
    for uid in (a_id, b_id):
        storage.record_phi_consent(
            user_id=uid,
            phi_consent_version=CURRENT_PHI_CONSENT_VERSION,
            ip_address="",
            user_agent="",
        )
    # B's child.
    b_child = storage.create_patient(
        Scope(user_id=b_id),
        first_name="B",
        last_name="Child",
        relationship="child",
        ehr_fhir_id="PAT-B",
    )

    user_a = _fake_user(a_id, "a@example.com")
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_current_user] = lambda: user_a
    try:
        client = TestClient(app)
        resp = client.post(f"/ehr/connect/epic/patient/{b_child.id}", follow_redirects=False)
        assert resp.status_code == 404
        events = storage.list_audit_events(actor_user_id=a_id, limit=10)
        assert any(e.action == "ehr.connect_patient.cross_tenant_attempt" for e in events)
    finally:
        app.dependency_overrides.clear()


def test_connect_patient_happy_path_stores_patient_scoped_connection(
    tmp_path, monkeypatch, epic_env
):
    storage, user_id, patient_id = _bootstrap(tmp_path, monkeypatch)
    user = _fake_user(user_id, "parent@example.com")
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        client = TestClient(app)
        resp1 = client.post(f"/ehr/connect/epic/patient/{patient_id}", follow_redirects=False)
        assert resp1.status_code == 303
        loc = resp1.headers["location"]
        assert loc.startswith("https://fake-epic.test/oauth2/authorize?")
        state = loc.split("state=")[1].split("&")[0]

        resp2 = client.get(f"/ehr/callback/epic?code=CODE&state={state}", follow_redirects=False)
        assert resp2.status_code == 303
        assert resp2.headers["location"] == f"/patients/{patient_id}"

        # Connection persisted patient-scoped.
        conn = storage.get_active_patient_ehr_connection(patient_id, "epic_sandbox")
        assert conn is not None
        assert conn.patient_id == patient_id
        assert conn.user_id is None
        assert conn.organization_id is None
        assert conn.patient_fhir_id == "PAT-99"

        # And NOT under the parent's user-scoped table.
        assert storage.get_active_ehr_connection(user_id, "epic_sandbox") is None

        events = storage.list_audit_events(actor_user_id=user_id, limit=10)
        assert any(e.action == "ehr.connected_patient" for e in events)
    finally:
        app.dependency_overrides.clear()
