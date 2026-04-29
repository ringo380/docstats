"""Route-level tests for eligibility check endpoints (Phase 11.B/C)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from docstats.auth import get_current_user
from docstats.availity_client import AvailityDisabledError
from docstats.phi import CURRENT_PHI_CONSENT_VERSION
from docstats.scope import Scope
from docstats.storage import Storage, get_storage
from docstats.web import app


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _fake_user(user_id: int, *, consent: bool = True) -> dict:
    return {
        "id": user_id,
        "email": "test@example.com",
        "display_name": None,
        "first_name": None,
        "last_name": None,
        "github_id": None,
        "github_login": None,
        "npi": None,
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


@pytest.fixture
def db(tmp_path: Path) -> Storage:
    storage = Storage(db_path=tmp_path / "test.db")
    return storage


@pytest.fixture
def solo_client(db: Storage):
    user_id = db.create_user("test@example.com", "hashed_pw")
    db.record_phi_consent(
        user_id=user_id,
        phi_consent_version=CURRENT_PHI_CONSENT_VERSION,
        ip_address="127.0.0.1",
        user_agent="pytest",
    )
    user = _fake_user(user_id)
    app.dependency_overrides[get_storage] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    client = TestClient(app, raise_server_exceptions=True)
    yield client, db, user_id
    app.dependency_overrides.clear()


def _seed_patient(db: Storage, user_id: int, *, dob: str | None = "1985-03-20") -> int:
    scope = Scope(user_id=user_id)
    patient = db.create_patient(
        scope,
        first_name="Alice",
        last_name="Smith",
        date_of_birth=dob,
        created_by_user_id=user_id,
    )
    return patient.id  # type: ignore[return-value]


def _seed_referral(db: Storage, user_id: int, patient_id: int) -> int:
    scope = Scope(user_id=user_id)
    referral = db.create_referral(
        scope,
        patient_id=patient_id,
        reason="Follow-up cardiology",
        urgency="routine",
        specialty_desc="Cardiology",
        created_by_user_id=user_id,
    )
    return referral.id  # type: ignore[return-value]


def _mock_availity_success() -> MagicMock:
    """Return a fake AvailityClient whose async_check_eligibility returns a coverage payload."""
    mock_client = MagicMock()
    mock_client.async_check_eligibility = AsyncMock(
        return_value={
            "coverages": [
                {
                    "subscriberPolicies": [
                        {
                            "plans": [
                                {
                                    "status": "1",  # active
                                    "planCode": "HMO",
                                }
                            ],
                            "copayAmount": 30.0,
                            "coinsurancePercent": 20.0,
                        }
                    ]
                }
            ]
        }
    )
    return mock_client


# ---------------------------------------------------------------------------
# Phase 11.B — patient-context eligibility endpoints
# ---------------------------------------------------------------------------


def test_trigger_eligibility_creates_check(solo_client, monkeypatch):
    client, db, user_id = solo_client
    patient_id = _seed_patient(db, user_id)
    monkeypatch.setattr("docstats.routes.eligibility.get_availity_client", _mock_availity_success)

    resp = client.post(
        f"/patients/{patient_id}/eligibility",
        data={"payer_id": "BCBSFL", "member_id": "MEM-001"},
    )
    assert resp.status_code == 200
    scope = Scope(user_id=user_id)
    check = db.get_latest_eligibility_check(scope, patient_id)
    assert check is not None
    assert check.status == "complete"
    assert check.availity_payer_id == "BCBSFL"


def test_trigger_eligibility_missing_dob_returns_error(solo_client, monkeypatch):
    client, db, user_id = solo_client
    patient_id = _seed_patient(db, user_id, dob=None)
    monkeypatch.setattr("docstats.routes.eligibility.get_availity_client", _mock_availity_success)

    resp = client.post(
        f"/patients/{patient_id}/eligibility",
        data={"payer_id": "BCBSFL", "member_id": "MEM-001"},
    )
    assert resp.status_code == 200
    scope = Scope(user_id=user_id)
    check = db.get_latest_eligibility_check(scope, patient_id)
    assert check is not None
    assert check.status == "error"
    assert "date of birth" in (check.error_message or "").lower()


def test_trigger_eligibility_availity_disabled(solo_client, monkeypatch):
    client, db, user_id = solo_client
    patient_id = _seed_patient(db, user_id)

    def _raise_disabled():
        raise AvailityDisabledError("not configured")

    monkeypatch.setattr("docstats.routes.eligibility.get_availity_client", _raise_disabled)

    resp = client.post(
        f"/patients/{patient_id}/eligibility",
        data={"payer_id": "BCBSFL", "member_id": "MEM-001"},
    )
    assert resp.status_code == 200
    scope = Scope(user_id=user_id)
    check = db.get_latest_eligibility_check(scope, patient_id)
    assert check is not None
    assert check.status == "error"


def test_trigger_eligibility_cooldown_active(solo_client, monkeypatch):
    client, db, user_id = solo_client
    patient_id = _seed_patient(db, user_id)

    # First call — succeeds and creates the check
    monkeypatch.setattr("docstats.routes.eligibility.get_availity_client", _mock_availity_success)
    resp1 = client.post(
        f"/patients/{patient_id}/eligibility",
        data={"payer_id": "BCBSFL", "member_id": "MEM-001"},
    )
    assert resp1.status_code == 200

    # Second call immediately — should hit cooldown (default 60s)
    resp2 = client.post(
        f"/patients/{patient_id}/eligibility",
        data={"payer_id": "BCBSFL", "member_id": "MEM-001"},
    )
    assert resp2.status_code == 200
    assert "wait" in resp2.text.lower() or "checked" in resp2.text.lower()


def test_get_latest_eligibility_returns_partial(solo_client, monkeypatch):
    client, db, user_id = solo_client
    patient_id = _seed_patient(db, user_id)

    # No check yet — should still return 200 (empty state)
    resp = client.get(f"/patients/{patient_id}/eligibility/latest")
    assert resp.status_code == 200

    # Create a check directly in storage
    scope = Scope(user_id=user_id)
    db.create_eligibility_check(
        scope,
        patient_id=patient_id,
        availity_payer_id="BCBSFL",
        service_type="30",
        status="complete",
    )
    resp2 = client.get(f"/patients/{patient_id}/eligibility/latest?payer_id=BCBSFL")
    assert resp2.status_code == 200


def test_trigger_eligibility_patient_not_found(solo_client):
    client, db, user_id = solo_client
    resp = client.post(
        "/patients/99999/eligibility",
        data={"payer_id": "X", "member_id": "Y"},
    )
    assert resp.status_code == 404


def test_get_latest_eligibility_patient_not_found(solo_client):
    client, db, user_id = solo_client
    resp = client.get("/patients/99999/eligibility/latest")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Phase 11.C — referral-context eligibility endpoints
# ---------------------------------------------------------------------------


def test_trigger_eligibility_from_referral(solo_client, monkeypatch):
    client, db, user_id = solo_client
    patient_id = _seed_patient(db, user_id)
    referral_id = _seed_referral(db, user_id, patient_id)
    monkeypatch.setattr("docstats.routes.eligibility.get_availity_client", _mock_availity_success)

    resp = client.post(
        f"/referrals/{referral_id}/eligibility",
        data={"payer_id": "AETNA", "member_id": "AET-999"},
    )
    assert resp.status_code == 200
    scope = Scope(user_id=user_id)
    check = db.get_latest_eligibility_check(scope, patient_id)
    assert check is not None
    assert check.status == "complete"
    assert check.availity_payer_id == "AETNA"


def test_trigger_eligibility_from_referral_no_dob(solo_client, monkeypatch):
    client, db, user_id = solo_client
    patient_id = _seed_patient(db, user_id, dob=None)
    referral_id = _seed_referral(db, user_id, patient_id)
    monkeypatch.setattr("docstats.routes.eligibility.get_availity_client", _mock_availity_success)

    resp = client.post(
        f"/referrals/{referral_id}/eligibility",
        data={"payer_id": "AETNA", "member_id": "AET-999"},
    )
    assert resp.status_code == 200
    scope = Scope(user_id=user_id)
    check = db.get_latest_eligibility_check(scope, patient_id)
    assert check is not None
    assert check.status == "error"
    assert "date of birth" in (check.error_message or "").lower()


def test_trigger_eligibility_from_referral_cooldown(solo_client, monkeypatch):
    client, db, user_id = solo_client
    patient_id = _seed_patient(db, user_id)
    referral_id = _seed_referral(db, user_id, patient_id)
    monkeypatch.setattr("docstats.routes.eligibility.get_availity_client", _mock_availity_success)

    # First call succeeds
    resp1 = client.post(
        f"/referrals/{referral_id}/eligibility",
        data={"payer_id": "AETNA", "member_id": "AET-999"},
    )
    assert resp1.status_code == 200

    # Immediate second call hits cooldown
    resp2 = client.post(
        f"/referrals/{referral_id}/eligibility",
        data={"payer_id": "AETNA", "member_id": "AET-999"},
    )
    assert resp2.status_code == 200
    assert "wait" in resp2.text.lower() or "checked" in resp2.text.lower()


def test_get_referral_eligibility_latest(solo_client, monkeypatch):
    client, db, user_id = solo_client
    patient_id = _seed_patient(db, user_id)
    referral_id = _seed_referral(db, user_id, patient_id)

    # No check yet
    resp = client.get(f"/referrals/{referral_id}/eligibility/latest")
    assert resp.status_code == 200

    # Seed one and re-fetch
    scope = Scope(user_id=user_id)
    db.create_eligibility_check(
        scope,
        patient_id=patient_id,
        availity_payer_id="AETNA",
        service_type="30",
        status="complete",
    )
    resp2 = client.get(f"/referrals/{referral_id}/eligibility/latest?payer_id=AETNA")
    assert resp2.status_code == 200


def test_trigger_eligibility_referral_not_found(solo_client):
    client, db, user_id = solo_client
    resp = client.post(
        "/referrals/99999/eligibility",
        data={"payer_id": "X", "member_id": "Y"},
    )
    assert resp.status_code == 404


def test_get_referral_eligibility_latest_not_found(solo_client):
    client, db, user_id = solo_client
    resp = client.get("/referrals/99999/eligibility/latest")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Scope isolation — user A cannot access user B's resources
# ---------------------------------------------------------------------------


def test_eligibility_scope_isolation(tmp_path: Path, monkeypatch):
    db = Storage(db_path=tmp_path / "test.db")
    user_a = db.create_user("a@example.com", "x")
    user_b = db.create_user("b@example.com", "x")
    for uid in (user_a, user_b):
        db.record_phi_consent(user_id=uid, phi_consent_version=CURRENT_PHI_CONSENT_VERSION, ip_address="127.0.0.1", user_agent="pytest")

    scope_a = Scope(user_id=user_a)
    patient_a = db.create_patient(scope_a, first_name="A", last_name="Patient", date_of_birth="1980-01-01", created_by_user_id=user_a)

    app.dependency_overrides[get_storage] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: _fake_user(user_b)
    client = TestClient(app, raise_server_exceptions=True)
    try:
        # user B trying to check eligibility on user A's patient
        resp = client.post(
            f"/patients/{patient_a.id}/eligibility",
            data={"payer_id": "X", "member_id": "Y"},
        )
        assert resp.status_code == 404
    finally:
        app.dependency_overrides.clear()
