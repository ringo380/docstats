"""Route-level tests for closed-loop response capture (Phase 7.A)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from docstats.auth import get_current_user
from docstats.phi import CURRENT_PHI_CONSENT_VERSION
from docstats.scope import Scope
from docstats.storage import Storage, get_storage
from docstats.web import app


def _fake_user(user_id: int, email: str = "a@example.com"):
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
        "phi_consent_at": "2026-01-01",
        "phi_consent_version": CURRENT_PHI_CONSENT_VERSION,
        "phi_consent_ip": None,
        "phi_consent_user_agent": None,
        "active_org_id": None,
    }


def _seed_referral(storage: Storage, user_id: int, *, status: str = "draft"):
    scope = Scope(user_id=user_id)
    patient = storage.create_patient(
        scope,
        first_name="Jane",
        last_name="Doe",
        date_of_birth="1980-01-01",
        created_by_user_id=user_id,
    )
    referral = storage.create_referral(
        scope,
        patient_id=patient.id,
        reason="Eval",
        specialty_desc="Cardiology",
        receiving_organization_name="Heart Clinic",
        created_by_user_id=user_id,
    )
    # Walk through the state machine when a non-draft initial status is needed.
    if status != "draft":
        path = {
            "ready": ["ready"],
            "sent": ["ready", "sent"],
            "scheduled": ["ready", "sent", "scheduled"],
            "awaiting_records": ["ready", "sent", "awaiting_records"],
        }[status]
        for s in path:
            storage.set_referral_status(scope, referral.id, s)
    return referral


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


# --- Create ---


def test_create_response_minimal(solo_client):
    client, storage, user_id = solo_client
    referral = _seed_referral(storage, user_id)
    resp = client.post(
        f"/referrals/{referral.id}/response",
        data={"appointment_date": "2026-05-01", "received_via": "fax"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/referrals/{referral.id}"
    scope = Scope(user_id=user_id)
    responses = storage.list_referral_responses(scope, referral.id)
    assert len(responses) == 1
    assert responses[0].appointment_date == "2026-05-01"
    assert responses[0].consult_completed is False
    assert responses[0].recorded_by_user_id == user_id


def test_create_response_emits_event(solo_client):
    client, storage, user_id = solo_client
    referral = _seed_referral(storage, user_id)
    client.post(
        f"/referrals/{referral.id}/response",
        data={"appointment_date": "2026-05-01", "received_via": "portal"},
        follow_redirects=False,
    )
    scope = Scope(user_id=user_id)
    events = storage.list_referral_events(scope, referral.id)
    response_events = [e for e in events if e.event_type == "response_received"]
    assert len(response_events) == 1
    assert response_events[0].to_value == "scheduled"
    assert response_events[0].note == "via portal"


def test_create_response_consult_completed_from_scheduled_auto_transitions(solo_client):
    """The headline closed-loop case: scheduled + consult_completed → completed."""
    client, storage, user_id = solo_client
    referral = _seed_referral(storage, user_id, status="scheduled")
    resp = client.post(
        f"/referrals/{referral.id}/response",
        data={
            "appointment_date": "2026-05-01",
            "received_via": "fax",
            "consult_completed": "on",
            "recommendations_text": "Start statin, f/u in 3 months.",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    scope = Scope(user_id=user_id)
    fresh = storage.get_referral(scope, referral.id)
    assert fresh.status == "completed"
    # status_changed event with auto-transition note
    events = storage.list_referral_events(scope, referral.id)
    auto = [e for e in events if e.event_type == "status_changed" and e.to_value == "completed"]
    assert len(auto) == 1
    assert "auto" in (auto[0].note or "")


def test_create_response_consult_completed_out_of_machine_skips_transition(solo_client):
    """consult_completed from draft/sent can't legally reach completed — the
    response is still recorded, but status stays put."""
    client, storage, user_id = solo_client
    referral = _seed_referral(storage, user_id, status="sent")
    resp = client.post(
        f"/referrals/{referral.id}/response",
        data={"received_via": "email", "consult_completed": "on"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    scope = Scope(user_id=user_id)
    fresh = storage.get_referral(scope, referral.id)
    assert fresh.status == "sent"  # unchanged
    responses = storage.list_referral_responses(scope, referral.id)
    assert len(responses) == 1
    assert responses[0].consult_completed is True


def test_create_response_empty_payload_rerenders_error(solo_client):
    """Blank appt + no recommendations + unchecked completed → form re-render."""
    client, storage, user_id = solo_client
    referral = _seed_referral(storage, user_id)
    resp = client.post(
        f"/referrals/{referral.id}/response",
        data={"received_via": "fax"},
    )
    assert resp.status_code == 200
    assert "Add an appointment date" in resp.text
    scope = Scope(user_id=user_id)
    assert storage.list_referral_responses(scope, referral.id) == []


def test_create_response_bad_received_via_returns_422(solo_client):
    client, storage, user_id = solo_client
    referral = _seed_referral(storage, user_id)
    resp = client.post(
        f"/referrals/{referral.id}/response",
        data={"received_via": "carrier_pigeon", "appointment_date": "2026-05-01"},
    )
    assert resp.status_code == 422


def test_create_response_bad_date_returns_422(solo_client):
    client, storage, user_id = solo_client
    referral = _seed_referral(storage, user_id)
    resp = client.post(
        f"/referrals/{referral.id}/response",
        data={"received_via": "fax", "appointment_date": "next tuesday"},
    )
    assert resp.status_code == 422


def test_create_response_unknown_referral_returns_404(solo_client):
    client, _, _ = solo_client
    resp = client.post(
        "/referrals/99999/response",
        data={"received_via": "fax", "appointment_date": "2026-05-01"},
    )
    assert resp.status_code == 404


# --- Update ---


def test_update_response_flipping_completed_auto_transitions(solo_client):
    """Edit an existing scheduled response to flip consult_completed → True,
    from a referral in 'scheduled' → referral auto-transitions to 'completed'."""
    client, storage, user_id = solo_client
    referral = _seed_referral(storage, user_id, status="scheduled")
    scope = Scope(user_id=user_id)
    prior = storage.record_referral_response(
        scope,
        referral.id,
        appointment_date="2026-05-01",
        received_via="phone",
        recorded_by_user_id=user_id,
    )
    assert prior is not None
    resp = client.post(
        f"/referrals/{referral.id}/response/{prior.id}",
        data={
            "received_via": "phone",
            "consult_completed": "on",
            "recommendations_text": "Done.",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    fresh = storage.get_referral(scope, referral.id)
    assert fresh.status == "completed"


def test_update_response_already_completed_does_not_double_transition(solo_client):
    """If consult_completed was already True, an edit that keeps it True should
    not emit a second auto status_changed event."""
    client, storage, user_id = solo_client
    referral = _seed_referral(storage, user_id, status="scheduled")
    scope = Scope(user_id=user_id)
    prior = storage.record_referral_response(
        scope,
        referral.id,
        appointment_date="2026-05-01",
        consult_completed=True,
        recommendations_text="Initial",
        received_via="fax",
        recorded_by_user_id=user_id,
    )
    assert prior is not None
    # Manually walk referral to completed (mimicking the prior create flow).
    storage.set_referral_status(scope, referral.id, "completed")
    pre_events = len(
        [
            e
            for e in storage.list_referral_events(scope, referral.id)
            if e.event_type == "status_changed"
        ]
    )
    # Edit the response — keep consult_completed=True, change recs text.
    resp = client.post(
        f"/referrals/{referral.id}/response/{prior.id}",
        data={
            "received_via": "fax",
            "consult_completed": "on",
            "recommendations_text": "Updated",
        },
    )
    assert resp.status_code in (200, 303)
    post_events = len(
        [
            e
            for e in storage.list_referral_events(scope, referral.id)
            if e.event_type == "status_changed"
        ]
    )
    assert post_events == pre_events  # no new status_changed


def test_update_response_unknown_returns_404(solo_client):
    client, storage, user_id = solo_client
    referral = _seed_referral(storage, user_id)
    resp = client.post(
        f"/referrals/{referral.id}/response/99999",
        data={"received_via": "fax"},
    )
    assert resp.status_code == 404


# --- Delete ---


def test_delete_response(solo_client):
    client, storage, user_id = solo_client
    referral = _seed_referral(storage, user_id)
    scope = Scope(user_id=user_id)
    r = storage.record_referral_response(
        scope, referral.id, received_via="fax", recorded_by_user_id=user_id
    )
    assert r is not None
    resp = client.delete(f"/referrals/{referral.id}/response/{r.id}")
    assert resp.status_code in (200, 303)
    assert storage.list_referral_responses(scope, referral.id) == []


def test_delete_response_unknown_returns_404(solo_client):
    client, storage, user_id = solo_client
    referral = _seed_referral(storage, user_id)
    resp = client.delete(f"/referrals/{referral.id}/response/99999")
    assert resp.status_code == 404


# --- Scope isolation ---


def test_cross_tenant_create_returns_404(tmp_path: Path):
    storage = Storage(db_path=tmp_path / "test.db")
    user_a = storage.create_user("a@example.com", "hashed")
    user_b = storage.create_user("b@example.com", "hashed")
    for uid in (user_a, user_b):
        storage.record_phi_consent(
            user_id=uid,
            phi_consent_version=CURRENT_PHI_CONSENT_VERSION,
            ip_address="",
            user_agent="",
        )
    scope_a = Scope(user_id=user_a)
    patient = storage.create_patient(
        scope_a, first_name="Jane", last_name="Doe", created_by_user_id=user_a
    )
    referral = storage.create_referral(
        scope_a, patient_id=patient.id, reason="X", created_by_user_id=user_a
    )
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_current_user] = lambda: _fake_user(user_b, email="b@example.com")
    try:
        client = TestClient(app)
        resp = client.post(
            f"/referrals/{referral.id}/response",
            data={"received_via": "fax", "appointment_date": "2026-05-01"},
        )
        assert resp.status_code == 404
    finally:
        app.dependency_overrides.clear()


# --- Audit ---


def test_audit_event_emitted_on_create(solo_client):
    client, storage, user_id = solo_client
    referral = _seed_referral(storage, user_id)
    client.post(
        f"/referrals/{referral.id}/response",
        data={"received_via": "fax", "appointment_date": "2026-05-01"},
    )
    rows = storage.list_audit_events(scope_user_id=user_id)
    actions = [r.action for r in rows]
    assert "referral.response.create" in actions
