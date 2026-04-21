"""Route-level tests for inline comments (Phase 7.B)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from docstats.auth import get_current_user
from docstats.phi import CURRENT_PHI_CONSENT_VERSION
from docstats.scope import Scope
from docstats.storage import Storage, get_storage
from docstats.web import app


def _fake_user(
    user_id: int,
    *,
    email: str = "a@example.com",
    first_name: str | None = None,
    last_name: str | None = None,
    display_name: str | None = None,
):
    return {
        "id": user_id,
        "email": email,
        "display_name": display_name,
        "first_name": first_name,
        "last_name": last_name,
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


def _seed_referral(storage: Storage, user_id: int):
    scope = Scope(user_id=user_id)
    patient = storage.create_patient(
        scope,
        first_name="Jane",
        last_name="Doe",
        created_by_user_id=user_id,
    )
    return storage.create_referral(
        scope,
        patient_id=patient.id,
        reason="Eval",
        specialty_desc="Cardiology",
        created_by_user_id=user_id,
    )


@pytest.fixture
def solo_client(tmp_path: Path):
    storage = Storage(db_path=tmp_path / "test.db")
    user_id = storage.create_user("a@example.com", "hashed_pw")
    # Populate first/last so the actor-map formatting is exercised by default.
    storage.update_user_profile(user_id, first_name="Alice", last_name="Smith")
    storage.record_phi_consent(
        user_id=user_id,
        phi_consent_version=CURRENT_PHI_CONSENT_VERSION,
        ip_address="",
        user_agent="",
    )
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_current_user] = lambda: _fake_user(
        user_id, first_name="Alice", last_name="Smith"
    )
    yield TestClient(app), storage, user_id
    app.dependency_overrides.clear()


# --- POST /notes happy path ---


def test_create_note_writes_event(solo_client):
    client, storage, user_id = solo_client
    referral = _seed_referral(storage, user_id)
    resp = client.post(
        f"/referrals/{referral.id}/notes",
        data={"note": "Patient is eager to get scheduled ASAP."},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"].startswith(f"/referrals/{referral.id}")
    scope = Scope(user_id=user_id)
    events = storage.list_referral_events(scope, referral.id)
    notes = [e for e in events if e.event_type == "note_added"]
    assert len(notes) == 1
    assert notes[0].note == "Patient is eager to get scheduled ASAP."
    assert notes[0].actor_user_id == user_id


def test_create_note_trims_whitespace(solo_client):
    client, storage, user_id = solo_client
    referral = _seed_referral(storage, user_id)
    client.post(
        f"/referrals/{referral.id}/notes",
        data={"note": "   has padding   "},
    )
    scope = Scope(user_id=user_id)
    notes = [
        e for e in storage.list_referral_events(scope, referral.id) if e.event_type == "note_added"
    ]
    assert notes[0].note == "has padding"


def test_create_note_empty_rerenders_error(solo_client):
    client, storage, user_id = solo_client
    referral = _seed_referral(storage, user_id)
    resp = client.post(f"/referrals/{referral.id}/notes", data={"note": "   "})
    assert resp.status_code == 200
    assert "Comment cannot be empty" in resp.text
    scope = Scope(user_id=user_id)
    notes = [
        e for e in storage.list_referral_events(scope, referral.id) if e.event_type == "note_added"
    ]
    assert notes == []


def test_create_note_missing_field_is_422(solo_client):
    """FastAPI ``Form(...)`` rejects an entirely absent field with 422 —
    matches the create referral / patient create surfaces."""
    client, storage, user_id = solo_client
    referral = _seed_referral(storage, user_id)
    resp = client.post(f"/referrals/{referral.id}/notes", data={})
    assert resp.status_code == 422


def test_create_note_unknown_referral_returns_404(solo_client):
    client, _, _ = solo_client
    resp = client.post("/referrals/99999/notes", data={"note": "ghost"})
    assert resp.status_code == 404


def test_create_note_emits_audit_action(solo_client):
    client, storage, user_id = solo_client
    referral = _seed_referral(storage, user_id)
    client.post(
        f"/referrals/{referral.id}/notes",
        data={"note": "Scheduler called, offered next Tuesday."},
    )
    rows = storage.list_audit_events(scope_user_id=user_id)
    note_events = [r for r in rows if r.action == "referral.note.create"]
    assert len(note_events) == 1
    assert note_events[0].metadata.get("length") == len("Scheduler called, offered next Tuesday.")


# --- Scope isolation ---


def test_cross_tenant_note_returns_404(tmp_path: Path):
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
        resp = client.post(f"/referrals/{referral.id}/notes", data={"note": "hijack"})
        assert resp.status_code == 404
        # B's write must not have landed in A's tenant.
        assert [
            e
            for e in storage.list_referral_events(scope_a, referral.id)
            if e.event_type == "note_added"
        ] == []
    finally:
        app.dependency_overrides.clear()


# --- Timeline rendering ---


def test_timeline_renders_note_with_actor_name(solo_client):
    client, storage, user_id = solo_client
    referral = _seed_referral(storage, user_id)
    client.post(
        f"/referrals/{referral.id}/notes",
        data={"note": "Waiting on lab report."},
    )
    resp = client.get(f"/referrals/{referral.id}")
    assert resp.status_code == 200
    html = resp.text
    assert "Waiting on lab report." in html
    # Actor pill should render the user's first + last name.
    assert "Alice Smith" in html
    # Dedicated note_added CSS class on the <li>.
    assert "event-note_added" in html


def test_timeline_actor_falls_back_to_display_name_then_email(tmp_path: Path):
    """Users with only display_name or only email still get a legible actor
    pill — no 'None' or 'undefined' in the rendered HTML."""
    storage = Storage(db_path=tmp_path / "test.db")
    uid = storage.create_user("eve@example.com", "hashed")
    storage.update_user_profile(uid, display_name="Eve DisplayOnly")
    storage.record_phi_consent(
        user_id=uid,
        phi_consent_version=CURRENT_PHI_CONSENT_VERSION,
        ip_address="",
        user_agent="",
    )
    scope = Scope(user_id=uid)
    patient = storage.create_patient(
        scope, first_name="Jane", last_name="Doe", created_by_user_id=uid
    )
    referral = storage.create_referral(
        scope, patient_id=patient.id, reason="X", created_by_user_id=uid
    )
    storage.record_referral_event(
        scope,
        referral.id,
        event_type="note_added",
        actor_user_id=uid,
        note="Hello",
    )

    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_current_user] = lambda: _fake_user(
        uid, email="eve@example.com", display_name="Eve DisplayOnly"
    )
    try:
        client = TestClient(app)
        resp = client.get(f"/referrals/{referral.id}")
        assert resp.status_code == 200
        assert "Eve DisplayOnly" in resp.text
    finally:
        app.dependency_overrides.clear()


def test_timeline_survives_deleted_actor(solo_client):
    """If an actor_user_id points at a user who was later removed, the
    ``referral_events.actor_user_id`` FK (ON DELETE SET NULL) nulls out the
    column and the timeline still renders without an actor pill."""
    client, storage, user_id = solo_client
    referral = _seed_referral(storage, user_id)
    scope = Scope(user_id=user_id)
    # Create a short-lived user, log an event as them, then hard-delete the
    # user so the FK nulls the actor_user_id column.
    ghost_id = storage.create_user("ghost@example.com", "hashed")
    storage.record_referral_event(
        scope,
        referral.id,
        event_type="note_added",
        actor_user_id=ghost_id,
        note="Ghost note",
    )
    storage._conn.execute("DELETE FROM users WHERE id = ?", (ghost_id,))
    storage._conn.commit()
    resp = client.get(f"/referrals/{referral.id}")
    assert resp.status_code == 200
    assert "Ghost note" in resp.text
