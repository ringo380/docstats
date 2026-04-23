"""Phase 9.A — Delivery route tests."""

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


def _seed(storage: Storage, user_id: int):
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
        reason="Chest pain",
        urgency="routine",
        specialty_desc="Cardiology",
        receiving_organization_name="Heart Clinic",
        created_by_user_id=user_id,
    )
    return scope, referral


@pytest.fixture
def client(tmp_path: Path):
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


def test_send_rejects_disabled_channel(client):
    tc, storage, user_id = client
    _, referral = _seed(storage, user_id)
    resp = tc.post(
        f"/referrals/{referral.id}/send",
        data={"channel": "fax", "recipient": "+14155551234"},
        follow_redirects=False,
    )
    # Every channel is disabled in 9.A; route-layer probe returns 422.
    assert resp.status_code == 422
    assert "fax" in resp.text
    # No delivery row created.
    assert storage.list_deliveries_for_referral(Scope(user_id=user_id), referral.id) == []


def test_send_rejects_unknown_channel(client):
    tc, storage, user_id = client
    _, referral = _seed(storage, user_id)
    resp = tc.post(
        f"/referrals/{referral.id}/send",
        data={"channel": "pigeon", "recipient": "x"},
        follow_redirects=False,
    )
    assert resp.status_code == 422


def test_send_rejects_missing_recipient(client):
    tc, storage, user_id = client
    _, referral = _seed(storage, user_id)
    resp = tc.post(
        f"/referrals/{referral.id}/send",
        data={"channel": "fax", "recipient": ""},
        follow_redirects=False,
    )
    assert resp.status_code == 422


def test_send_404_on_missing_referral(client):
    tc, _, _ = client
    resp = tc.post(
        "/referrals/999999/send",
        data={"channel": "fax", "recipient": "+14155551234"},
        follow_redirects=False,
    )
    assert resp.status_code == 404


def test_cancel_404_on_cross_scope(client, tmp_path: Path):
    tc, storage, user_id = client
    scope, referral = _seed(storage, user_id)
    delivery = storage.create_delivery(
        scope,
        referral_id=referral.id,
        channel="fax",
        recipient="+14155551234",
    )
    # Swap to another user.
    other = storage.create_user("other@example.com", "hashed")
    storage.record_phi_consent(
        user_id=other,
        phi_consent_version=CURRENT_PHI_CONSENT_VERSION,
        ip_address="",
        user_agent="",
    )
    app.dependency_overrides[get_current_user] = lambda: _fake_user(other, "other@example.com")
    resp = tc.post(
        f"/referrals/{referral.id}/deliveries/{delivery.id}/cancel",
        follow_redirects=False,
    )
    assert resp.status_code == 404


def test_cancel_happy_path(client):
    tc, storage, user_id = client
    scope, referral = _seed(storage, user_id)
    delivery = storage.create_delivery(
        scope,
        referral_id=referral.id,
        channel="fax",
        recipient="+14155551234",
    )
    resp = tc.post(
        f"/referrals/{referral.id}/deliveries/{delivery.id}/cancel",
        follow_redirects=False,
    )
    assert resp.status_code == 303
    refreshed = storage.get_delivery(scope, delivery.id)
    assert refreshed is not None
    assert refreshed.status == "cancelled"


def test_detail_page_shows_no_channels_configured(client, monkeypatch):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    tc, storage, user_id = client
    _, referral = _seed(storage, user_id)
    resp = tc.get(f"/referrals/{referral.id}")
    assert resp.status_code == 200
    # Send card renders the "no channels" message, not the form.
    assert "No delivery channels are configured" in resp.text
    # Delivery log renders (empty state).
    assert "No deliveries yet" in resp.text


def test_detail_page_shows_existing_deliveries(client):
    tc, storage, user_id = client
    scope, referral = _seed(storage, user_id)
    storage.create_delivery(
        scope,
        referral_id=referral.id,
        channel="fax",
        recipient="+14155551234",
    )
    resp = tc.get(f"/referrals/{referral.id}")
    assert resp.status_code == 200
    assert "+14155551234" in resp.text
    assert "Queued" in resp.text or "queued" in resp.text.lower()
