"""Route-level tests for prior-auth endpoints (Phase 11.E)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
    return Storage(db_path=tmp_path / "test.db")


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


def _seed_referral(db: Storage, user_id: int, *, dob: str | None = "1985-03-20") -> tuple[int, int]:
    scope = Scope(user_id=user_id)
    patient = db.create_patient(
        scope,
        first_name="Alice",
        last_name="Smith",
        date_of_birth=dob,
        created_by_user_id=user_id,
    )
    referral = db.create_referral(
        scope,
        patient_id=patient.id,  # type: ignore[arg-type]
        reason="Cardiology consult",
        urgency="routine",
        specialty_desc="Cardiology",
        created_by_user_id=user_id,
    )
    return patient.id, referral.id  # type: ignore[return-value]


def _mock_submit_pending() -> MagicMock:
    mc = MagicMock()
    mc.async_submit_authorization = AsyncMock(return_value={"id": "AUTH-1", "status": "pending"})
    mc.async_get_authorization_status = AsyncMock(
        return_value={"id": "AUTH-1", "status": "pending"}
    )
    return mc


def _mock_submit_approved() -> MagicMock:
    mc = MagicMock()
    mc.async_submit_authorization = AsyncMock(
        return_value={
            "id": "AUTH-7",
            "status": "approved",
            "referenceNumber": "AUTH-XYZ",
            "decisionDate": "2026-05-05T10:30:00Z",
        }
    )
    return mc


# ---------------------------------------------------------------------------
# POST /referrals/{id}/auth-submit
# ---------------------------------------------------------------------------


def _submit_payload() -> dict:
    return {
        "payer_id": "BCBSM",
        "payer_name": "Blue Cross",
        "member_id": "MEM-1",
        "service_type": "30",
        "procedure_codes": "99213, 73721",
        "diagnosis_codes": "M54.5",
        "service_date": "2026-06-01",
        "place_of_service": "11",
    }


def test_submit_creates_pending_row(solo_client, monkeypatch):
    client, db, user_id = solo_client
    _, referral_id = _seed_referral(db, user_id)
    monkeypatch.setattr("docstats.routes.prior_auth.get_availity_client", _mock_submit_pending)

    resp = client.post(f"/referrals/{referral_id}/auth-submit", data=_submit_payload())
    assert resp.status_code == 200
    scope = Scope(user_id=user_id)
    sub = db.get_latest_prior_auth_submission(scope, referral_id)
    assert sub is not None
    assert sub.status == "submitted"
    assert sub.availity_submission_id == "AUTH-1"
    assert sub.idempotency_key is not None
    assert "99213" in sub.procedure_codes
    assert "73721" in sub.procedure_codes


def test_submit_decoded_approved(solo_client, monkeypatch):
    client, db, user_id = solo_client
    _, referral_id = _seed_referral(db, user_id)
    monkeypatch.setattr("docstats.routes.prior_auth.get_availity_client", _mock_submit_approved)

    resp = client.post(f"/referrals/{referral_id}/auth-submit", data=_submit_payload())
    assert resp.status_code == 200
    sub = db.get_latest_prior_auth_submission(Scope(user_id=user_id), referral_id)
    assert sub is not None
    assert sub.status == "approved"
    assert sub.reference_number == "AUTH-XYZ"


def test_submit_double_blocked_when_in_flight(solo_client, monkeypatch):
    client, db, user_id = solo_client
    _, referral_id = _seed_referral(db, user_id)
    monkeypatch.setattr("docstats.routes.prior_auth.get_availity_client", _mock_submit_pending)

    client.post(f"/referrals/{referral_id}/auth-submit", data=_submit_payload())
    resp2 = client.post(f"/referrals/{referral_id}/auth-submit", data=_submit_payload())
    assert resp2.status_code == 200
    assert "already in flight" in resp2.text.lower()
    # Still only one row
    rows = db.list_prior_auth_submissions(Scope(user_id=user_id), referral_id)
    assert len(rows) == 1


def test_submit_missing_dob_returns_error(solo_client, monkeypatch):
    client, db, user_id = solo_client
    _, referral_id = _seed_referral(db, user_id, dob=None)
    monkeypatch.setattr("docstats.routes.prior_auth.get_availity_client", _mock_submit_pending)

    resp = client.post(f"/referrals/{referral_id}/auth-submit", data=_submit_payload())
    assert resp.status_code == 200
    assert "date of birth" in resp.text.lower()
    # No row created — error short-circuits before insert
    sub = db.get_latest_prior_auth_submission(Scope(user_id=user_id), referral_id)
    assert sub is None


def test_submit_missing_procedure_codes_returns_error(solo_client, monkeypatch):
    client, db, user_id = solo_client
    _, referral_id = _seed_referral(db, user_id)
    monkeypatch.setattr("docstats.routes.prior_auth.get_availity_client", _mock_submit_pending)

    payload = _submit_payload()
    payload["procedure_codes"] = "  ,  "  # whitespace/comma only
    resp = client.post(f"/referrals/{referral_id}/auth-submit", data=payload)
    assert resp.status_code == 200
    assert "procedure code" in resp.text.lower()


def test_submit_availity_disabled_marks_error(solo_client, monkeypatch):
    client, db, user_id = solo_client
    _, referral_id = _seed_referral(db, user_id)

    def _raise_disabled():
        raise AvailityDisabledError("not configured")

    monkeypatch.setattr("docstats.routes.prior_auth.get_availity_client", _raise_disabled)

    resp = client.post(f"/referrals/{referral_id}/auth-submit", data=_submit_payload())
    assert resp.status_code == 200
    sub = db.get_latest_prior_auth_submission(Scope(user_id=user_id), referral_id)
    assert sub is not None
    assert sub.status == "error"


# ---------------------------------------------------------------------------
# POST /referrals/{id}/auth-status/refresh
# ---------------------------------------------------------------------------


def test_refresh_with_no_submission_returns_error(solo_client):
    client, db, user_id = solo_client
    _, referral_id = _seed_referral(db, user_id)
    resp = client.post(f"/referrals/{referral_id}/auth-status/refresh")
    assert resp.status_code == 200
    assert "no prior-auth" in resp.text.lower()


def test_refresh_terminal_status_does_not_call_api(solo_client, monkeypatch):
    client, db, user_id = solo_client
    _, referral_id = _seed_referral(db, user_id)

    # Seed an approved row directly
    scope = Scope(user_id=user_id)
    sub = db.create_prior_auth_submission(
        scope,
        referral_id=referral_id,
        availity_payer_id="BCBSM",
        member_id="MEM-1",
        service_type="30",
        diagnosis_codes=["M54.5"],
        procedure_codes=["99213"],
        status="approved",
    )
    db.update_prior_auth_submission(sub.id, availity_submission_id="AUTH-1")  # type: ignore[arg-type]

    called = {"n": 0}

    def _factory():
        called["n"] += 1
        return _mock_submit_pending()

    monkeypatch.setattr("docstats.routes.prior_auth.get_availity_client", _factory)

    resp = client.post(f"/referrals/{referral_id}/auth-status/refresh")
    assert resp.status_code == 200
    assert "terminal" in resp.text.lower()
    assert called["n"] == 0


def test_refresh_polls_when_submitted(solo_client, monkeypatch):
    client, db, user_id = solo_client
    _, referral_id = _seed_referral(db, user_id)

    scope = Scope(user_id=user_id)
    sub = db.create_prior_auth_submission(
        scope,
        referral_id=referral_id,
        availity_payer_id="BCBSM",
        member_id="MEM-1",
        service_type="30",
        diagnosis_codes=["M54.5"],
        procedure_codes=["99213"],
        status="submitted",
    )
    db.update_prior_auth_submission(sub.id, availity_submission_id="AUTH-1")  # type: ignore[arg-type]

    mc = MagicMock()
    mc.async_get_authorization_status = AsyncMock(
        return_value={
            "id": "AUTH-1",
            "status": "approved",
            "referenceNumber": "REF-99",
            "decisionDate": "2026-05-05T10:30:00Z",
        }
    )
    monkeypatch.setattr("docstats.routes.prior_auth.get_availity_client", lambda: mc)

    resp = client.post(f"/referrals/{referral_id}/auth-status/refresh")
    assert resp.status_code == 200
    updated = db.get_latest_prior_auth_submission(scope, referral_id)
    assert updated is not None
    assert updated.status == "approved"
    assert updated.reference_number == "REF-99"


def test_refresh_cooldown_short_circuits(solo_client, monkeypatch):
    client, db, user_id = solo_client
    _, referral_id = _seed_referral(db, user_id)

    scope = Scope(user_id=user_id)
    sub = db.create_prior_auth_submission(
        scope,
        referral_id=referral_id,
        availity_payer_id="BCBSM",
        member_id="MEM-1",
        service_type="30",
        diagnosis_codes=["M54.5"],
        procedure_codes=["99213"],
        status="submitted",
    )
    recent = datetime.now(tz=timezone.utc) - timedelta(seconds=5)
    db.update_prior_auth_submission(  # type: ignore[arg-type]
        sub.id, availity_submission_id="AUTH-1", last_polled_at=recent
    )

    called = {"n": 0}

    def _factory():
        called["n"] += 1
        return _mock_submit_pending()

    monkeypatch.setattr("docstats.routes.prior_auth.get_availity_client", _factory)

    resp = client.post(f"/referrals/{referral_id}/auth-status/refresh")
    assert resp.status_code == 200
    assert "wait" in resp.text.lower()
    assert called["n"] == 0


def test_refresh_without_availity_id_short_circuits(solo_client, monkeypatch):
    client, db, user_id = solo_client
    _, referral_id = _seed_referral(db, user_id)
    scope = Scope(user_id=user_id)
    db.create_prior_auth_submission(
        scope,
        referral_id=referral_id,
        availity_payer_id="BCBSM",
        member_id="MEM-1",
        service_type="30",
        diagnosis_codes=["M54.5"],
        procedure_codes=["99213"],
        status="pending",
    )
    called = {"n": 0}

    def _factory():
        called["n"] += 1
        return _mock_submit_pending()

    monkeypatch.setattr("docstats.routes.prior_auth.get_availity_client", _factory)

    resp = client.post(f"/referrals/{referral_id}/auth-status/refresh")
    assert resp.status_code == 200
    assert "not yet acknowledged" in resp.text.lower()
    assert called["n"] == 0


# ---------------------------------------------------------------------------
# GET /referrals/{id}/auth-status
# ---------------------------------------------------------------------------


def test_get_status_renders_card_with_no_submission(solo_client):
    client, db, user_id = solo_client
    _, referral_id = _seed_referral(db, user_id)
    resp = client.get(f"/referrals/{referral_id}/auth-status")
    assert resp.status_code == 200
    assert "prior authorization" in resp.text.lower()


def test_get_status_404_for_unknown_referral(solo_client):
    client, _, _ = solo_client
    resp = client.get("/referrals/999999/auth-status")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Cross-scope guard
# ---------------------------------------------------------------------------


def test_cross_scope_referral_not_visible(solo_client):
    client, db, user_id = solo_client
    # Create a second user; their referral must not be visible to user 1.
    other = db.create_user("other@example.com", "x")
    db.record_phi_consent(
        user_id=other,
        phi_consent_version=CURRENT_PHI_CONSENT_VERSION,
        ip_address="127.0.0.1",
        user_agent="pytest",
    )
    other_scope = Scope(user_id=other)
    p = db.create_patient(
        other_scope,
        first_name="Bob",
        last_name="Jones",
        date_of_birth="1970-01-01",
        created_by_user_id=other,
    )
    r = db.create_referral(
        other_scope,
        patient_id=p.id,  # type: ignore[arg-type]
        reason="X",
        urgency="routine",
        specialty_desc="Cardiology",
        created_by_user_id=other,
    )
    resp = client.get(f"/referrals/{r.id}/auth-status")
    assert resp.status_code == 404
