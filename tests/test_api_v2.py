"""Phase 8.B — API v2 read endpoint tests.

Covers:

- Content negotiation (plain JSON by default, FHIR Bundle when Accept contains
  ``fhir+json``; wildcard ``*/*`` stays plain JSON — pins the substring-check
  simplification so a future real Accept parser surfaces in CI).
- Auth behavior — unauthenticated callers get 401 JSON, NOT 303 redirect.
  Regression for the AuthRequiredException gap surfaced during Phase 8 design.
- PHI consent — authenticated-but-not-consented callers get 403 JSON.
- Scope isolation — cross-tenant returns 404 JSON.
- Patient endpoint returns a bare Patient resource (not a Bundle).
- Audit trail records the accept header + chosen content type.
- ``X-Docstats-Api-Version: 2`` present on every response.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from docstats.auth import get_current_user
from docstats.phi import CURRENT_PHI_CONSENT_VERSION
from docstats.scope import Scope
from docstats.storage import Storage, get_storage
from docstats.web import app


def _fake_user(user_id: int, email: str = "a@example.com", *, consent: bool = True):
    return {
        "id": user_id,
        "email": email,
        "display_name": None,
        "first_name": "Coordinator",
        "last_name": "Tester",
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
        "active_org_id": None,
    }


def _seed_referral(storage: Storage, user_id: int):
    scope = Scope(user_id=user_id)
    patient = storage.create_patient(
        scope,
        first_name="Jane",
        last_name="Doe",
        date_of_birth="1980-05-15",
        created_by_user_id=user_id,
    )
    referral = storage.create_referral(
        scope,
        patient_id=patient.id,
        reason="Chest pain eval",
        urgency="urgent",
        specialty_desc="Cardiology",
        receiving_organization_name="Heart Clinic",
        created_by_user_id=user_id,
    )
    return patient, referral


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
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_current_user] = lambda: _fake_user(user_id)
    yield TestClient(app), storage, user_id
    app.dependency_overrides.clear()


# ---------- Auth gap regression ----------


def test_referral_unauthenticated_returns_401_json_not_redirect(tmp_path: Path):
    """API v2 MUST NOT 303-redirect unauthenticated callers to /auth/login.

    Machine consumers can't follow redirects to interactive pages. The
    shipped require_user_api dependency must raise HTTPException(401) so
    the default JSON-serialization path returns a usable error body.
    """
    storage = Storage(db_path=tmp_path / "test.db")
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_current_user] = lambda: None
    try:
        client = TestClient(app)
        resp = client.get("/api/v2/referrals/1", follow_redirects=False)
        assert resp.status_code == 401
        assert resp.headers["content-type"].startswith("application/json")
        body = resp.json()
        assert body["detail"]["code"] == "authentication_required"
    finally:
        app.dependency_overrides.clear()


def test_patient_unauthenticated_returns_401_json_not_redirect(tmp_path: Path):
    storage = Storage(db_path=tmp_path / "test.db")
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_current_user] = lambda: None
    try:
        client = TestClient(app)
        resp = client.get("/api/v2/patients/1", follow_redirects=False)
        assert resp.status_code == 401
    finally:
        app.dependency_overrides.clear()


# ---------- Content negotiation ----------


def test_referral_returns_plain_json_by_default(solo_client):
    client, storage, user_id = solo_client
    _, referral = _seed_referral(storage, user_id)

    resp = client.get(f"/api/v2/referrals/{referral.id}")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.headers["x-docstats-api-version"] == "2"
    body = resp.json()
    # Plain JSON mirrors the Referral pydantic model directly — no FHIR
    # Bundle wrapping.
    assert body["id"] == referral.id
    assert body["reason"] == "Chest pain eval"
    assert "resourceType" not in body


def test_referral_returns_fhir_bundle_when_accept_header_set(solo_client):
    client, storage, user_id = solo_client
    _, referral = _seed_referral(storage, user_id)

    resp = client.get(
        f"/api/v2/referrals/{referral.id}",
        headers={"Accept": "application/fhir+json"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/fhir+json")
    body = resp.json()
    assert body["resourceType"] == "Bundle"
    assert body["type"] == "document"
    types = [e["resource"]["resourceType"] for e in body["entry"]]
    assert types[0] == "Patient"
    assert types[1] == "ServiceRequest"


def test_referral_wildcard_accept_returns_plain_json(solo_client):
    """``Accept: */*`` must default to plain JSON — pins the substring-
    ``fhir+json`` simplification. Any future real RFC 7231 parser will
    break this test first."""
    client, storage, user_id = solo_client
    _, referral = _seed_referral(storage, user_id)

    resp = client.get(f"/api/v2/referrals/{referral.id}", headers={"Accept": "*/*"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    body = resp.json()
    assert "resourceType" not in body


def test_referral_mixed_accept_header_picks_fhir_when_substring_present(solo_client):
    client, storage, user_id = solo_client
    _, referral = _seed_referral(storage, user_id)

    resp = client.get(
        f"/api/v2/referrals/{referral.id}",
        headers={"Accept": "text/html, application/fhir+json;q=0.9, application/json;q=0.5"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/fhir+json")


# ---------- Scope + errors ----------


def test_referral_404_when_missing(solo_client):
    client, _, _ = solo_client
    resp = client.get("/api/v2/referrals/999999")
    assert resp.status_code == 404
    assert resp.json()["code"] == "not-found"


def test_referral_404_returns_operation_outcome_in_fhir_mode(solo_client):
    client, _, _ = solo_client
    resp = client.get("/api/v2/referrals/999999", headers={"Accept": "application/fhir+json"})
    assert resp.status_code == 404
    body = resp.json()
    assert body["resourceType"] == "OperationOutcome"
    assert body["issue"][0]["code"] == "not-found"


def test_referral_cross_tenant_returns_404(tmp_path: Path):
    storage = Storage(db_path=tmp_path / "test.db")
    user_a = storage.create_user("a@example.com", "hashed_pw")
    user_b = storage.create_user("b@example.com", "hashed_pw")
    for uid in (user_a, user_b):
        storage.record_phi_consent(
            user_id=uid,
            phi_consent_version=CURRENT_PHI_CONSENT_VERSION,
            ip_address="",
            user_agent="",
        )
    _, referral_a = _seed_referral(storage, user_a)

    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_current_user] = lambda: _fake_user(user_b, "b@example.com")
    try:
        client = TestClient(app)
        resp = client.get(f"/api/v2/referrals/{referral_a.id}")
        assert resp.status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_referral_no_phi_consent_returns_403(tmp_path: Path):
    storage = Storage(db_path=tmp_path / "test.db")
    user_id = storage.create_user("a@example.com", "hashed_pw")
    # Deliberately do NOT call record_phi_consent.

    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_current_user] = lambda: _fake_user(user_id, consent=False)
    try:
        client = TestClient(app)
        resp = client.get("/api/v2/referrals/1", follow_redirects=False)
        assert resp.status_code == 403
        assert resp.headers["content-type"].startswith("application/json")
        assert resp.json()["detail"]["code"] == "phi_consent_required"
    finally:
        app.dependency_overrides.clear()


# ---------- /api/v2/patients/{id} ----------


def test_patient_endpoint_returns_flat_json_by_default(solo_client):
    client, storage, user_id = solo_client
    patient, _ = _seed_referral(storage, user_id)

    resp = client.get(f"/api/v2/patients/{patient.id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == patient.id
    assert body["first_name"] == "Jane"
    assert "resourceType" not in body


def test_patient_endpoint_returns_resource_only_not_bundle_in_fhir_mode(solo_client):
    """fhir+json mode returns a bare Patient resource — not a Bundle."""
    client, storage, user_id = solo_client
    patient, _ = _seed_referral(storage, user_id)

    resp = client.get(
        f"/api/v2/patients/{patient.id}",
        headers={"Accept": "application/fhir+json"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["resourceType"] == "Patient"
    assert body["resourceType"] != "Bundle"


def test_patient_404_when_missing(solo_client):
    client, _, _ = solo_client
    resp = client.get("/api/v2/patients/999999")
    assert resp.status_code == 404


# ---------- Audit + version header ----------


def test_audit_records_referral_api_v2_read_with_metadata(solo_client):
    client, storage, user_id = solo_client
    _, referral = _seed_referral(storage, user_id)

    resp = client.get(
        f"/api/v2/referrals/{referral.id}",
        headers={"Accept": "application/fhir+json"},
    )
    assert resp.status_code == 200

    audit_rows = storage.list_audit_events(limit=20)
    matching = [a for a in audit_rows if a.action == "referral.api_v2.read"]
    assert matching, "no referral.api_v2.read audit row emitted"
    meta = matching[0].metadata
    assert meta["content_type"] == "application/fhir+json"
    assert "fhir+json" in meta["accept_header"]
    assert meta["bundle_entries"] > 0


def test_audit_records_patient_api_v2_read(solo_client):
    client, storage, user_id = solo_client
    patient, _ = _seed_referral(storage, user_id)

    resp = client.get(f"/api/v2/patients/{patient.id}")
    assert resp.status_code == 200

    audit_rows = storage.list_audit_events(limit=20)
    assert any(a.action == "patient.api_v2.read" for a in audit_rows)


def test_api_version_header_present_on_all_responses(solo_client):
    client, storage, user_id = solo_client
    _, referral = _seed_referral(storage, user_id)

    for path, headers in (
        (f"/api/v2/referrals/{referral.id}", {}),
        (f"/api/v2/referrals/{referral.id}", {"Accept": "application/fhir+json"}),
        ("/api/v2/referrals/999999", {}),  # 404 path must also carry the header
    ):
        resp = client.get(path, headers=headers)
        assert resp.headers.get("x-docstats-api-version") == "2", (
            f"missing X-Docstats-Api-Version on {path!r} with headers {headers!r}"
        )
