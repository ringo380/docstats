"""Route-level tests for assignment + nav badge + assignee filter (Phase 7.C)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from docstats.auth import get_current_user
from docstats.phi import CURRENT_PHI_CONSENT_VERSION
from docstats.scope import Scope
from docstats.storage import Storage, get_storage
from docstats.web import app


def _fake_user(user_id: int, *, email: str = "a@example.com", active_org_id: int | None = None):
    return {
        "id": user_id,
        "email": email,
        "display_name": None,
        "first_name": "Alice",
        "last_name": "Smith",
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
        "active_org_id": active_org_id,
    }


def _seed_referral(storage: Storage, user_id: int, org_id: int | None = None):
    scope = Scope(
        user_id=user_id, organization_id=org_id, membership_role="coordinator" if org_id else None
    )
    patient = storage.create_patient(
        scope, first_name="Jane", last_name="Doe", created_by_user_id=user_id
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
    storage.update_user_profile(user_id, first_name="Alice", last_name="Smith")
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


@pytest.fixture
def org_client(tmp_path: Path):
    """Two-member org scope. Returns (client, storage, owner_id, member_id, org_id)."""
    storage = Storage(db_path=tmp_path / "test.db")
    owner = storage.create_user("owner@example.com", "hashed_pw")
    member = storage.create_user("member@example.com", "hashed_pw")
    storage.update_user_profile(owner, first_name="Olivia", last_name="Owner")
    storage.update_user_profile(member, first_name="Mia", last_name="Member")
    for uid in (owner, member):
        storage.record_phi_consent(
            user_id=uid,
            phi_consent_version=CURRENT_PHI_CONSENT_VERSION,
            ip_address="",
            user_agent="",
        )
    org = storage.create_organization(name="Clinic", slug="clinic")
    storage.create_membership(organization_id=org.id, user_id=owner, role="owner")
    storage.create_membership(organization_id=org.id, user_id=member, role="coordinator")
    storage.set_active_org(owner, org.id)

    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_current_user] = lambda: _fake_user(
        owner, email="owner@example.com", active_org_id=org.id
    )
    yield TestClient(app), storage, owner, member, org.id
    app.dependency_overrides.clear()


# --- Self-assign in solo mode ---


def test_solo_self_assign(solo_client):
    client, storage, user_id = solo_client
    referral = _seed_referral(storage, user_id)
    resp = client.post(
        f"/referrals/{referral.id}/assign",
        data={"user_id": "me"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    scope = Scope(user_id=user_id)
    fresh = storage.get_referral(scope, referral.id)
    assert fresh.assigned_to_user_id == user_id


def test_solo_numeric_self_assign(solo_client):
    client, storage, user_id = solo_client
    referral = _seed_referral(storage, user_id)
    resp = client.post(
        f"/referrals/{referral.id}/assign",
        data={"user_id": str(user_id)},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert storage.get_referral(Scope(user_id=user_id), referral.id).assigned_to_user_id == user_id


def test_solo_cannot_assign_to_other_user(solo_client):
    """Assigning to a user_id that isn't the caller is rejected in solo mode."""
    client, storage, user_id = solo_client
    # Create a second user who is NOT in the caller's scope.
    other = storage.create_user("other@example.com", "hashed")
    referral = _seed_referral(storage, user_id)
    resp = client.post(
        f"/referrals/{referral.id}/assign",
        data={"user_id": str(other)},
    )
    assert resp.status_code == 422


def test_unassign_via_empty(solo_client):
    client, storage, user_id = solo_client
    referral = _seed_referral(storage, user_id)
    # First assign, then unassign.
    client.post(f"/referrals/{referral.id}/assign", data={"user_id": "me"})
    resp = client.post(
        f"/referrals/{referral.id}/assign", data={"user_id": ""}, follow_redirects=False
    )
    assert resp.status_code == 303
    fresh = storage.get_referral(Scope(user_id=user_id), referral.id)
    assert fresh.assigned_to_user_id is None


def test_assign_emits_events_and_audit(solo_client):
    client, storage, user_id = solo_client
    referral = _seed_referral(storage, user_id)
    client.post(f"/referrals/{referral.id}/assign", data={"user_id": "me"})
    client.post(f"/referrals/{referral.id}/assign", data={"user_id": ""})

    scope = Scope(user_id=user_id)
    events = storage.list_referral_events(scope, referral.id)
    assigned = [e for e in events if e.event_type == "assigned"]
    unassigned = [e for e in events if e.event_type == "unassigned"]
    assert len(assigned) == 1
    assert assigned[0].to_value == str(user_id)
    assert len(unassigned) == 1

    audit_actions = [r.action for r in storage.list_audit_events(scope_user_id=user_id)]
    assert "referral.assigned" in audit_actions
    assert "referral.unassigned" in audit_actions


def test_assign_noop_does_not_write_event(solo_client):
    """POSTing the same target as the current assignee must not emit a second
    assigned event — avoids event-log noise from double-clicks."""
    client, storage, user_id = solo_client
    referral = _seed_referral(storage, user_id)
    client.post(f"/referrals/{referral.id}/assign", data={"user_id": "me"})
    scope = Scope(user_id=user_id)
    first = len(
        [e for e in storage.list_referral_events(scope, referral.id) if e.event_type == "assigned"]
    )
    client.post(f"/referrals/{referral.id}/assign", data={"user_id": "me"})
    second = len(
        [e for e in storage.list_referral_events(scope, referral.id) if e.event_type == "assigned"]
    )
    assert first == second == 1


def test_assign_invalid_user_id_returns_422(solo_client):
    client, storage, user_id = solo_client
    referral = _seed_referral(storage, user_id)
    for bogus in ("abc", "-1"):
        resp = client.post(f"/referrals/{referral.id}/assign", data={"user_id": bogus})
        assert resp.status_code == 422, f"{bogus!r} should be rejected"


def test_assign_unknown_referral_returns_404(solo_client):
    client, _, _ = solo_client
    resp = client.post("/referrals/99999/assign", data={"user_id": "me"})
    assert resp.status_code == 404


# --- Org mode: assign to another member ---


def test_org_owner_assigns_member(org_client):
    client, storage, owner, member, org_id = org_client
    scope = Scope(user_id=owner, organization_id=org_id, membership_role="owner")
    patient = storage.create_patient(
        scope, first_name="Jane", last_name="Doe", created_by_user_id=owner
    )
    referral = storage.create_referral(
        scope, patient_id=patient.id, reason="Eval", created_by_user_id=owner
    )
    resp = client.post(
        f"/referrals/{referral.id}/assign",
        data={"user_id": str(member)},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    fresh = storage.get_referral(scope, referral.id)
    assert fresh.assigned_to_user_id == member


def test_org_cannot_assign_to_outside_user(org_client):
    client, storage, owner, member, org_id = org_client
    outsider = storage.create_user("outsider@example.com", "hashed")
    scope = Scope(user_id=owner, organization_id=org_id, membership_role="owner")
    patient = storage.create_patient(
        scope, first_name="Jane", last_name="Doe", created_by_user_id=owner
    )
    referral = storage.create_referral(
        scope, patient_id=patient.id, reason="Eval", created_by_user_id=owner
    )
    resp = client.post(
        f"/referrals/{referral.id}/assign",
        data={"user_id": str(outsider)},
    )
    assert resp.status_code == 422


def test_org_cannot_assign_soft_deleted_member(org_client):
    client, storage, owner, member, org_id = org_client
    storage.soft_delete_membership(org_id, member)
    scope = Scope(user_id=owner, organization_id=org_id, membership_role="owner")
    patient = storage.create_patient(
        scope, first_name="Jane", last_name="Doe", created_by_user_id=owner
    )
    referral = storage.create_referral(
        scope, patient_id=patient.id, reason="Eval", created_by_user_id=owner
    )
    resp = client.post(
        f"/referrals/{referral.id}/assign",
        data={"user_id": str(member)},
    )
    assert resp.status_code == 422


def test_detail_keeps_soft_deleted_current_assignee_selected(org_client):
    client, storage, owner, member, org_id = org_client
    scope = Scope(user_id=owner, organization_id=org_id, membership_role="owner")
    patient = storage.create_patient(
        scope, first_name="Jane", last_name="Doe", created_by_user_id=owner
    )
    referral = storage.create_referral(
        scope,
        patient_id=patient.id,
        reason="Eval",
        assigned_to_user_id=member,
        created_by_user_id=owner,
    )
    storage.soft_delete_membership(org_id, member)

    resp = client.get(f"/referrals/{referral.id}")
    assert resp.status_code == 200
    assert f'<option value="{member}" selected' in resp.text
    assert "Mia Member (off-team)" in resp.text


# --- Workspace filter ---


def test_assignee_me_filter_narrows_workspace(solo_client):
    client, storage, user_id = solo_client
    scope = Scope(user_id=user_id)
    # Two referrals; only one assigned to me.
    p = storage.create_patient(scope, first_name="A", last_name="B", created_by_user_id=user_id)
    r1 = storage.create_referral(scope, patient_id=p.id, reason="r1", created_by_user_id=user_id)
    r2 = storage.create_referral(scope, patient_id=p.id, reason="r2", created_by_user_id=user_id)
    storage.update_referral(scope, r1.id, assigned_to_user_id=user_id)
    # r2 left unassigned

    resp = client.get("/referrals?assignee=me")
    assert resp.status_code == 200
    # Row link only renders for r1 (the one assigned to me).
    assert f"/referrals/{r1.id}" in resp.text
    assert f"/referrals/{r2.id}" not in resp.text


def test_assignee_me_filter_with_no_params_shows_all(solo_client):
    """Sanity check: without the filter, both are visible."""
    client, storage, user_id = solo_client
    scope = Scope(user_id=user_id)
    p = storage.create_patient(scope, first_name="A", last_name="B", created_by_user_id=user_id)
    r1 = storage.create_referral(scope, patient_id=p.id, reason="r1", created_by_user_id=user_id)
    r2 = storage.create_referral(scope, patient_id=p.id, reason="r2", created_by_user_id=user_id)
    storage.update_referral(scope, r1.id, assigned_to_user_id=user_id)
    resp = client.get("/referrals")
    assert f"/referrals/{r1.id}" in resp.text
    assert f"/referrals/{r2.id}" in resp.text


def test_assignee_me_checkbox_renders_checked(solo_client):
    client, _, _ = solo_client
    resp = client.get("/referrals?assignee=me")
    assert resp.status_code == 200
    assert 'name="assignee" value="me" checked' in resp.text.replace("\n", " ")


def test_assignee_numeric_via_param(solo_client):
    client, storage, user_id = solo_client
    scope = Scope(user_id=user_id)
    p = storage.create_patient(scope, first_name="A", last_name="B", created_by_user_id=user_id)
    r = storage.create_referral(
        scope, patient_id=p.id, reason="visible", created_by_user_id=user_id
    )
    storage.update_referral(scope, r.id, assigned_to_user_id=user_id)

    resp = client.get(f"/referrals?assignee={user_id}")
    assert resp.status_code == 200
    assert f"/referrals/{r.id}" in resp.text


# --- Nav badge ---


def test_nav_badge_zero_when_no_assignments(solo_client):
    client, _, _ = solo_client
    resp = client.get("/referrals")
    assert resp.status_code == 200
    assert "nav-badge-assigned" not in resp.text


def test_nav_badge_shows_open_count(solo_client):
    client, storage, user_id = solo_client
    scope = Scope(user_id=user_id)
    p = storage.create_patient(scope, first_name="A", last_name="B", created_by_user_id=user_id)
    r1 = storage.create_referral(scope, patient_id=p.id, reason="r1", created_by_user_id=user_id)
    r2 = storage.create_referral(scope, patient_id=p.id, reason="r2", created_by_user_id=user_id)
    r3 = storage.create_referral(scope, patient_id=p.id, reason="r3", created_by_user_id=user_id)
    for r in (r1, r2, r3):
        storage.update_referral(scope, r.id, assigned_to_user_id=user_id)
    # r3 terminal should NOT count
    storage.set_referral_status(scope, r3.id, "cancelled")

    resp = client.get("/referrals")
    assert resp.status_code == 200
    assert "nav-badge-assigned" in resp.text
    # Badge rendered with count 2
    assert ">2<" in resp.text


def test_nav_badge_hidden_for_terminal_only(solo_client):
    client, storage, user_id = solo_client
    scope = Scope(user_id=user_id)
    p = storage.create_patient(scope, first_name="A", last_name="B", created_by_user_id=user_id)
    r = storage.create_referral(scope, patient_id=p.id, reason="done", created_by_user_id=user_id)
    storage.update_referral(scope, r.id, assigned_to_user_id=user_id)
    storage.set_referral_status(scope, r.id, "cancelled")

    resp = client.get("/referrals")
    assert resp.status_code == 200
    assert "nav-badge-assigned" not in resp.text


def test_nav_badge_counts_open_referrals_before_limit(solo_client):
    client, storage, user_id = solo_client
    scope = Scope(user_id=user_id)
    p = storage.create_patient(scope, first_name="A", last_name="B", created_by_user_id=user_id)
    open_referral = storage.create_referral(
        scope, patient_id=p.id, reason="open", created_by_user_id=user_id
    )
    storage.update_referral(scope, open_referral.id, assigned_to_user_id=user_id)
    for i in range(201):
        referral = storage.create_referral(
            scope, patient_id=p.id, reason=f"done {i}", created_by_user_id=user_id
        )
        storage.update_referral(scope, referral.id, assigned_to_user_id=user_id)
        storage.set_referral_status(scope, referral.id, "cancelled")

    resp = client.get("/referrals")
    assert resp.status_code == 200
    assert "nav-badge-assigned" in resp.text
    assert ">1<" in resp.text


def test_nav_badge_is_available_on_profile_page(solo_client):
    client, storage, user_id = solo_client
    scope = Scope(user_id=user_id)
    p = storage.create_patient(scope, first_name="A", last_name="B", created_by_user_id=user_id)
    referral = storage.create_referral(
        scope, patient_id=p.id, reason="assigned", created_by_user_id=user_id
    )
    storage.update_referral(scope, referral.id, assigned_to_user_id=user_id)

    resp = client.get("/profile")
    assert resp.status_code == 200
    assert "nav-badge-assigned" in resp.text
    assert ">1<" in resp.text


# --- Cross-tenant ---


def test_cross_tenant_assign_returns_404(tmp_path: Path):
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
        resp = client.post(f"/referrals/{referral.id}/assign", data={"user_id": "me"})
        assert resp.status_code == 404
    finally:
        app.dependency_overrides.clear()
