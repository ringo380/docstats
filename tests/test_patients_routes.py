"""Route-level tests for patients CRUD (Phase 2.A)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from docstats.auth import get_current_user
from docstats.phi import CURRENT_PHI_CONSENT_VERSION
from docstats.storage import Storage, get_storage
from docstats.web import app


def _fake_user(user_id: int, email: str, *, consent: bool = True, active_org_id: int | None = None):
    return {
        "id": user_id,
        "email": email,
        "display_name": None,
        "first_name": None,
        "last_name": None,
        "github_id": None,
        "github_login": None,
        "password_hash": "hashed_pw",
        "created_at": "2026-01-01",
        "last_login_at": None,
        "terms_accepted_at": "2026-01-01",
        "phi_consent_at": "2026-01-01" if consent else None,
        "phi_consent_version": CURRENT_PHI_CONSENT_VERSION if consent else None,
        "phi_consent_ip": None,
        "phi_consent_user_agent": None,
        "active_org_id": active_org_id,
    }


@pytest.fixture
def solo_client(tmp_path: Path):
    storage = Storage(db_path=tmp_path / "test.db")
    user_id = storage.create_user("a@example.com", "hashed_pw")
    storage.record_phi_consent(
        user_id=user_id,
        phi_consent_version=CURRENT_PHI_CONSENT_VERSION,
        ip_address="",
        user_agent="",
    )
    user = _fake_user(user_id, "a@example.com")
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_current_user] = lambda: user
    yield TestClient(app), storage, user_id
    app.dependency_overrides.clear()


@pytest.fixture
def no_consent_client(tmp_path: Path):
    storage = Storage(db_path=tmp_path / "test.db")
    user_id = storage.create_user("b@example.com", "hashed_pw")
    user = _fake_user(user_id, "b@example.com", consent=False)
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_current_user] = lambda: user
    yield TestClient(app), storage, user_id
    app.dependency_overrides.clear()


# --- List / new form ---


def test_list_empty(solo_client):
    client, _, _ = solo_client
    resp = client.get("/patients")
    assert resp.status_code == 200
    assert "No patients yet" in resp.text


def test_new_form_renders(solo_client):
    client, _, _ = solo_client
    resp = client.get("/patients/new")
    assert resp.status_code == 200
    assert "First name" in resp.text
    assert "Last name" in resp.text


def test_phi_consent_required_blocks_list(no_consent_client):
    client, _, _ = no_consent_client
    resp = client.get("/patients", follow_redirects=False)
    # PhiConsentRequiredException -> AuthRequiredException -> redirect to /auth/login
    assert resp.status_code in (302, 303, 307)
    assert "/auth/login" in resp.headers.get("location", "")


# --- Create ---


def test_create_patient(solo_client):
    client, storage, user_id = solo_client
    resp = client.post(
        "/patients",
        data={"first_name": "Jane", "last_name": "Doe", "date_of_birth": "1980-05-15"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    loc = resp.headers["location"]
    assert loc.startswith("/patients/")
    patient_id = int(loc.rsplit("/", 1)[-1])
    from docstats.scope import Scope

    patient = storage.get_patient(Scope(user_id=user_id), patient_id)
    assert patient is not None
    assert patient.first_name == "Jane"
    assert patient.date_of_birth == "1980-05-15"
    # Audit row written
    events = storage.list_audit_events(limit=5)
    assert any(e.action == "patient.create" for e in events)


def test_create_patient_hx_redirect(solo_client):
    client, _, _ = solo_client
    resp = client.post(
        "/patients",
        data={"first_name": "Jane", "last_name": "Doe"},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert resp.headers["hx-redirect"].startswith("/patients/")


def test_create_missing_required(solo_client):
    client, _, _ = solo_client
    # Empty strings hit FastAPI's Form(...) missing-field validator → 422.
    resp = client.post("/patients", data={"first_name": "", "last_name": ""})
    assert resp.status_code == 422


def test_create_whitespace_only_name(solo_client):
    client, _, _ = solo_client
    # Whitespace passes the Form(...) boundary but the route rejects it.
    resp = client.post(
        "/patients",
        data={"first_name": "   ", "last_name": "   "},
    )
    assert resp.status_code == 200
    assert "required" in resp.text.lower()


def test_create_invalid_dob(solo_client):
    client, _, _ = solo_client
    resp = client.post(
        "/patients",
        data={"first_name": "Jane", "last_name": "Doe", "date_of_birth": "not-a-date"},
    )
    assert resp.status_code == 422


# --- Detail / update ---


def test_detail_renders(solo_client):
    client, storage, user_id = solo_client
    from docstats.scope import Scope

    p = storage.create_patient(
        Scope(user_id=user_id),
        first_name="Jane",
        last_name="Doe",
        created_by_user_id=user_id,
    )
    resp = client.get(f"/patients/{p.id}")
    assert resp.status_code == 200
    assert "Jane" in resp.text and "Doe" in resp.text


def test_detail_not_found(solo_client):
    client, _, _ = solo_client
    resp = client.get("/patients/99999")
    assert resp.status_code == 404


def test_update_patient(solo_client):
    client, storage, user_id = solo_client
    from docstats.scope import Scope

    p = storage.create_patient(
        Scope(user_id=user_id),
        first_name="Jane",
        last_name="Doe",
        created_by_user_id=user_id,
    )
    resp = client.post(
        f"/patients/{p.id}",
        data={"first_name": "Janet", "last_name": "Doe", "phone": "555-1234"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    updated = storage.get_patient(Scope(user_id=user_id), p.id)
    assert updated.first_name == "Janet"
    assert updated.phone == "555-1234"


# --- Delete ---


def test_delete_patient(solo_client):
    client, storage, user_id = solo_client
    from docstats.scope import Scope

    p = storage.create_patient(
        Scope(user_id=user_id),
        first_name="Jane",
        last_name="Doe",
        created_by_user_id=user_id,
    )
    resp = client.delete(f"/patients/{p.id}", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/patients"
    # Soft-deleted — default list excludes it
    assert storage.get_patient(Scope(user_id=user_id), p.id) is None


# --- Cross-tenant scope isolation ---


def test_cross_user_isolation(tmp_path: Path):
    """User A cannot read user B's patient via the route."""
    storage = Storage(db_path=tmp_path / "test.db")
    uid_a = storage.create_user("a@example.com", "hashed")
    uid_b = storage.create_user("b@example.com", "hashed")
    for uid in (uid_a, uid_b):
        storage.record_phi_consent(
            user_id=uid,
            phi_consent_version=CURRENT_PHI_CONSENT_VERSION,
            ip_address="",
            user_agent="",
        )
    from docstats.scope import Scope

    p_b = storage.create_patient(
        Scope(user_id=uid_b),
        first_name="Bob",
        last_name="Private",
        created_by_user_id=uid_b,
    )

    user_a = _fake_user(uid_a, "a@example.com")
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_current_user] = lambda: user_a
    try:
        client = TestClient(app)
        resp = client.get(f"/patients/{p_b.id}")
        assert resp.status_code == 404
        # Listing shows none of B's patients
        resp = client.get("/patients")
        assert "Bob Private" not in resp.text
        # Update should also 404
        resp = client.post(
            f"/patients/{p_b.id}",
            data={"first_name": "X", "last_name": "Y"},
        )
        assert resp.status_code == 404
        # Delete too
        resp = client.delete(f"/patients/{p_b.id}")
        assert resp.status_code == 404
    finally:
        app.dependency_overrides.clear()
