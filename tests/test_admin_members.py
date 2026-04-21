"""Route-level tests for /admin/members + /invite (Phase 6.F)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from docstats.auth import get_current_user
from docstats.domain.invitations import (
    DEFAULT_INVITATION_TTL_SECONDS,
    compute_expires_at,
    generate_token,
)
from docstats.storage import Storage, get_storage
from docstats.web import app


def _fake_user(
    user_id: int,
    email: str,
    *,
    active_org_id: int | None = None,
    is_org_admin: bool = False,
) -> dict:
    return {
        "id": user_id,
        "email": email,
        "display_name": None,
        "first_name": None,
        "last_name": None,
        "github_id": None,
        "github_login": None,
        "password_hash": "hashed",
        "created_at": "2026-01-01",
        "last_login_at": None,
        "terms_accepted_at": "2026-01-01",
        "active_org_id": active_org_id,
        "is_org_admin": is_org_admin,
    }


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    return Storage(db_path=tmp_path / "admin_members.db")


@pytest.fixture
def org_admin(storage: Storage):
    """Org with admin + owner + staff seeded."""
    admin_id = storage.create_user("admin@example.com", "hashed")
    owner_id = storage.create_user("owner@example.com", "hashed")
    staff_id = storage.create_user("staff@example.com", "hashed")
    org = storage.create_organization(name="Acme Clinic", slug="acme")
    storage.create_membership(organization_id=org.id, user_id=owner_id, role="owner")
    storage.create_membership(organization_id=org.id, user_id=admin_id, role="admin")
    storage.create_membership(organization_id=org.id, user_id=staff_id, role="staff")
    storage.set_active_org(admin_id, org.id)
    user = _fake_user(admin_id, "admin@example.com", active_org_id=org.id, is_org_admin=True)
    return {
        "admin_id": admin_id,
        "owner_id": owner_id,
        "staff_id": staff_id,
        "org": org,
        "user": user,
    }


def _client(storage: Storage, user: dict | None) -> TestClient:
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


def _cleanup() -> None:
    app.dependency_overrides.clear()


# --- Role enforcement ---


def test_members_list_rejects_solo_user(storage: Storage) -> None:
    uid = storage.create_user("solo@example.com", "hashed")
    user = _fake_user(uid, "solo@example.com", active_org_id=None)
    try:
        resp = _client(storage, user).get("/admin/members")
        assert resp.status_code == 403
    finally:
        _cleanup()


@pytest.mark.parametrize("role", ["read_only", "staff", "clinician", "coordinator"])
def test_members_list_rejects_sub_admin(storage: Storage, role: str) -> None:
    uid = storage.create_user(f"{role}@example.com", "hashed")
    org = storage.create_organization(name="R", slug=f"r-{role}")
    storage.create_membership(organization_id=org.id, user_id=uid, role=role)
    storage.set_active_org(uid, org.id)
    user = _fake_user(uid, f"{role}@example.com", active_org_id=org.id, is_org_admin=False)
    try:
        resp = _client(storage, user).get("/admin/members")
        assert resp.status_code == 403
    finally:
        _cleanup()


# --- List view ---


def test_list_shows_active_members(storage: Storage, org_admin) -> None:
    try:
        resp = _client(storage, org_admin["user"]).get("/admin/members")
        assert resp.status_code == 200
        body = resp.text
        assert "owner@example.com" in body
        assert "admin@example.com" in body
        assert "staff@example.com" in body
        # Admin is viewing; their row is marked as self.
        assert "(you)" in body
    finally:
        _cleanup()


def test_list_shows_pending_invitations(storage: Storage, org_admin) -> None:
    storage.create_invitation(
        organization_id=org_admin["org"].id,
        email="invitee@example.com",
        role="staff",
        token=generate_token(),
        expires_at=compute_expires_at(DEFAULT_INVITATION_TTL_SECONDS),
        invited_by_user_id=org_admin["admin_id"],
    )
    try:
        resp = _client(storage, org_admin["user"]).get("/admin/members")
        body = resp.text
        assert "invitee@example.com" in body
        assert "Pending invitations (1)" in body
    finally:
        _cleanup()


# --- Invite ---


def test_invite_creates_pending_and_shows_link(storage: Storage, org_admin) -> None:
    try:
        resp = _client(storage, org_admin["user"]).post(
            "/admin/members/invite",
            data={"email": "NEW@example.com", "role": "coordinator"},
        )
        assert resp.status_code == 200
        body = resp.text
        assert "Invitation created for new@example.com" in body
        # Magic link rendered (absolute URL with /invite/<token>).
        assert "/invite/" in body
        # Storage side: exactly one pending invitation, email lowercased.
        invs = storage.list_invitations_for_org(org_admin["org"].id)
        assert len(invs) == 1
        assert invs[0].email == "new@example.com"
        assert invs[0].role == "coordinator"
        events = storage.list_audit_events(scope_organization_id=org_admin["org"].id)
        assert any(e.action == "admin.member.invite" for e in events)
    finally:
        _cleanup()


def test_invite_rejects_existing_active_member(storage: Storage, org_admin) -> None:
    try:
        resp = _client(storage, org_admin["user"]).post(
            "/admin/members/invite",
            data={"email": "staff@example.com", "role": "coordinator"},
        )
        assert resp.status_code == 200
        assert "already an active member" in resp.text
    finally:
        _cleanup()


def test_invite_rejects_duplicate_pending(storage: Storage, org_admin) -> None:
    storage.create_invitation(
        organization_id=org_admin["org"].id,
        email="dup@example.com",
        role="staff",
        token=generate_token(),
        expires_at=compute_expires_at(DEFAULT_INVITATION_TTL_SECONDS),
    )
    try:
        resp = _client(storage, org_admin["user"]).post(
            "/admin/members/invite",
            data={"email": "dup@example.com", "role": "staff"},
        )
        assert resp.status_code == 200
        assert "pending invitation already exists" in resp.text
    finally:
        _cleanup()


def test_invite_rejects_bad_email(storage: Storage, org_admin) -> None:
    try:
        resp = _client(storage, org_admin["user"]).post(
            "/admin/members/invite",
            data={"email": "not-an-email", "role": "staff"},
        )
        assert resp.status_code == 200
        assert "valid email" in resp.text.lower()
    finally:
        _cleanup()


def test_invite_rejects_bad_role(storage: Storage, org_admin) -> None:
    try:
        resp = _client(storage, org_admin["user"]).post(
            "/admin/members/invite",
            data={"email": "new@example.com", "role": "god"},
        )
        assert resp.status_code == 200
        assert "Unknown role" in resp.text
    finally:
        _cleanup()


# --- Revoke invitation ---


def test_revoke_invitation(storage: Storage, org_admin) -> None:
    inv = storage.create_invitation(
        organization_id=org_admin["org"].id,
        email="x@example.com",
        role="staff",
        token=generate_token(),
        expires_at=compute_expires_at(DEFAULT_INVITATION_TTL_SECONDS),
    )
    try:
        resp = _client(storage, org_admin["user"]).post(
            f"/admin/members/invitations/{inv.id}/revoke", follow_redirects=False
        )
        assert resp.status_code == 303
        fetched = storage.get_invitation(inv.id)
        assert fetched is not None
        assert fetched.revoked_at is not None
    finally:
        _cleanup()


def test_revoke_cross_tenant_returns_404(storage: Storage, org_admin) -> None:
    other_org = storage.create_organization(name="Other", slug="other")
    foreign_inv = storage.create_invitation(
        organization_id=other_org.id,
        email="x@example.com",
        role="staff",
        token=generate_token(),
        expires_at=compute_expires_at(DEFAULT_INVITATION_TTL_SECONDS),
    )
    try:
        resp = _client(storage, org_admin["user"]).post(
            f"/admin/members/invitations/{foreign_inv.id}/revoke"
        )
        assert resp.status_code == 404
    finally:
        _cleanup()


# --- Role change ---


def test_change_role_staff_to_coordinator(storage: Storage, org_admin) -> None:
    try:
        resp = _client(storage, org_admin["user"]).post(
            f"/admin/members/{org_admin['staff_id']}/role",
            data={"role": "coordinator"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        m = storage.get_membership(org_admin["org"].id, org_admin["staff_id"])
        assert m is not None and m.role == "coordinator"
    finally:
        _cleanup()


def test_change_role_rejects_unknown_role(storage: Storage, org_admin) -> None:
    try:
        resp = _client(storage, org_admin["user"]).post(
            f"/admin/members/{org_admin['staff_id']}/role",
            data={"role": "god"},
        )
        assert resp.status_code == 422
    finally:
        _cleanup()


def test_cannot_demote_sole_owner(storage: Storage, org_admin) -> None:
    try:
        # Only one owner exists; demoting should be blocked.
        resp = _client(storage, org_admin["user"]).post(
            f"/admin/members/{org_admin['owner_id']}/role",
            data={"role": "admin"},
        )
        assert resp.status_code == 200
        assert "sole owner" in resp.text
        m = storage.get_membership(org_admin["org"].id, org_admin["owner_id"])
        assert m is not None and m.role == "owner"
    finally:
        _cleanup()


def test_can_demote_owner_when_another_owner_exists(storage: Storage, org_admin) -> None:
    # Promote the staff user to owner so the last-owner guard no longer fires.
    storage.update_membership_role(org_admin["org"].id, org_admin["staff_id"], "owner")
    try:
        resp = _client(storage, org_admin["user"]).post(
            f"/admin/members/{org_admin['owner_id']}/role",
            data={"role": "admin"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        m = storage.get_membership(org_admin["org"].id, org_admin["owner_id"])
        assert m is not None and m.role == "admin"
    finally:
        _cleanup()


# --- Remove member ---


def test_remove_member(storage: Storage, org_admin) -> None:
    try:
        resp = _client(storage, org_admin["user"]).post(
            f"/admin/members/{org_admin['staff_id']}/remove",
            follow_redirects=False,
        )
        assert resp.status_code == 303
        m = storage.get_membership(org_admin["org"].id, org_admin["staff_id"])
        # Membership filtered out by get_membership when soft-deleted.
        assert m is None
    finally:
        _cleanup()


def test_cannot_remove_sole_owner(storage: Storage, org_admin) -> None:
    try:
        resp = _client(storage, org_admin["user"]).post(
            f"/admin/members/{org_admin['owner_id']}/remove"
        )
        assert resp.status_code == 200
        assert "sole owner" in resp.text
        m = storage.get_membership(org_admin["org"].id, org_admin["owner_id"])
        assert m is not None
    finally:
        _cleanup()


def test_cannot_remove_self_when_sole_admin(storage: Storage) -> None:
    """Edge case: admin is the sole admin/owner (no other privileged
    members). Removing self would orphan console access."""
    admin_id = storage.create_user("solo-admin@example.com", "hashed")
    org = storage.create_organization(name="OneMan", slug="oneman")
    storage.create_membership(organization_id=org.id, user_id=admin_id, role="admin")
    storage.set_active_org(admin_id, org.id)
    user = _fake_user(admin_id, "solo-admin@example.com", active_org_id=org.id, is_org_admin=True)
    try:
        resp = _client(storage, user).post(f"/admin/members/{admin_id}/remove")
        assert resp.status_code == 200
        assert "only admin/owner" in resp.text
    finally:
        _cleanup()


# --- /invite redemption ---


def test_invite_landing_404_on_bad_token(storage: Storage) -> None:
    try:
        resp = _client(storage, None).get("/invite/nonexistenttoken1234567890")
        assert resp.status_code == 404
    finally:
        _cleanup()


def test_invite_landing_anonymous_shows_login_prompt(storage: Storage, org_admin) -> None:
    token = generate_token()
    storage.create_invitation(
        organization_id=org_admin["org"].id,
        email="newbie@example.com",
        role="staff",
        token=token,
        expires_at=compute_expires_at(DEFAULT_INVITATION_TTL_SECONDS),
    )
    try:
        resp = _client(storage, None).get(f"/invite/{token}")
        assert resp.status_code == 200
        body = resp.text
        assert "newbie@example.com" in body
        assert "/auth/login" in body
        assert "/auth/signup" in body
    finally:
        _cleanup()


def test_invite_landing_email_mismatch_renders_warning(storage: Storage, org_admin) -> None:
    token = generate_token()
    storage.create_invitation(
        organization_id=org_admin["org"].id,
        email="invited@example.com",
        role="staff",
        token=token,
        expires_at=compute_expires_at(DEFAULT_INVITATION_TTL_SECONDS),
    )
    # Admin from fixture is signed in as a DIFFERENT email.
    try:
        resp = _client(storage, org_admin["user"]).get(f"/invite/{token}")
        assert resp.status_code == 200
        body = resp.text
        assert "invited@example.com" in body
        assert "sent to" in body

    finally:
        _cleanup()


def test_invite_accept_happy_path(storage: Storage, org_admin) -> None:
    # Create a new user whose email matches the invitation, then accept.
    new_user_id = storage.create_user("newbie@example.com", "hashed")
    token = generate_token()
    storage.create_invitation(
        organization_id=org_admin["org"].id,
        email="newbie@example.com",
        role="coordinator",
        token=token,
        expires_at=compute_expires_at(DEFAULT_INVITATION_TTL_SECONDS),
    )
    newbie_user = _fake_user(new_user_id, "newbie@example.com", active_org_id=None)
    try:
        resp = _client(storage, newbie_user).post(f"/invite/{token}/accept", follow_redirects=False)
        assert resp.status_code == 303
        # Membership created.
        m = storage.get_membership(org_admin["org"].id, new_user_id)
        assert m is not None
        assert m.role == "coordinator"
        # Invitation marked accepted.
        inv_by_token = storage.get_invitation_by_token(token)
        assert inv_by_token is not None
        assert inv_by_token.accepted_at is not None
        # active_org_id set.
        fetched = storage.get_user_by_id(new_user_id)
        assert fetched is not None
        assert fetched["active_org_id"] == org_admin["org"].id
        # Audit recorded.
        events = storage.list_audit_events(scope_organization_id=org_admin["org"].id)
        assert any(e.action == "admin.member.joined" for e in events)
    finally:
        _cleanup()


def test_invite_accept_rejects_email_mismatch(storage: Storage, org_admin) -> None:
    token = generate_token()
    storage.create_invitation(
        organization_id=org_admin["org"].id,
        email="invited@example.com",
        role="staff",
        token=token,
        expires_at=compute_expires_at(DEFAULT_INVITATION_TTL_SECONDS),
    )
    try:
        resp = _client(storage, org_admin["user"]).post(f"/invite/{token}/accept")
        # No membership created; render returns 200 with a warning.
        assert resp.status_code == 200
        assert "Please sign in as that user" in resp.text
        # Invitation remains pending.
        still_pending = storage.get_invitation_by_token(token)
        assert still_pending is not None
        assert still_pending.is_pending() is True
    finally:
        _cleanup()


def test_invite_accept_rejects_revoked(storage: Storage, org_admin) -> None:
    new_user_id = storage.create_user("late@example.com", "hashed")
    token = generate_token()
    inv = storage.create_invitation(
        organization_id=org_admin["org"].id,
        email="late@example.com",
        role="staff",
        token=token,
        expires_at=compute_expires_at(DEFAULT_INVITATION_TTL_SECONDS),
    )
    storage.revoke_invitation(inv.id)
    late_user = _fake_user(new_user_id, "late@example.com", active_org_id=None)
    try:
        resp = _client(storage, late_user).post(f"/invite/{token}/accept")
        assert resp.status_code == 200
        assert "revoked" in resp.text
        # No membership created.
        m = storage.get_membership(org_admin["org"].id, new_user_id)
        assert m is None
    finally:
        _cleanup()


def test_invite_accept_rejects_expired(storage: Storage, org_admin) -> None:
    """Force an expired invitation via direct SQL and verify accept refuses."""
    new_user_id = storage.create_user("tardy@example.com", "hashed")
    token = generate_token()
    past = datetime.now(tz=timezone.utc) - timedelta(hours=1)
    storage._conn.execute(
        "INSERT INTO organization_invitations (organization_id, email, role, token, expires_at) "
        "VALUES (?, ?, ?, ?, datetime(?))",
        (
            org_admin["org"].id,
            "tardy@example.com",
            "staff",
            token,
            past.strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    storage._conn.commit()
    tardy_user = _fake_user(new_user_id, "tardy@example.com", active_org_id=None)
    try:
        resp = _client(storage, tardy_user).post(f"/invite/{token}/accept")
        assert resp.status_code == 200
        assert "expired" in resp.text
        m = storage.get_membership(org_admin["org"].id, new_user_id)
        assert m is None
    finally:
        _cleanup()
