"""Route-level tests for the admin console (Phase 6.A)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from docstats.auth import get_current_user
from docstats.storage import Storage, get_storage
from docstats.web import app


def _fake_user(
    user_id: int,
    email: str,
    *,
    active_org_id: int | None = None,
    is_org_admin: bool = False,
) -> dict:
    """Match the shape of ``Storage.get_user_by_id`` + the ``is_org_admin``
    enrichment added by ``get_current_user``. Other fields kept minimal —
    admin routes don't touch password/terms/consent columns.
    """
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
    return Storage(db_path=tmp_path / "admin.db")


def _client_with(storage: Storage, user: dict) -> TestClient:
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


def _cleanup() -> None:
    app.dependency_overrides.clear()


# --- Role enforcement ---


def test_admin_overview_rejects_anonymous(storage: Storage) -> None:
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_current_user] = lambda: None
    try:
        client = TestClient(app)
        resp = client.get("/admin", follow_redirects=False)
        # AuthRequiredException → 303 to /auth/login.
        assert resp.status_code == 303
        assert resp.headers["location"] == "/auth/login"
    finally:
        _cleanup()


def test_admin_overview_rejects_solo_user(storage: Storage) -> None:
    user_id = storage.create_user("solo@example.com", "hashed")
    user = _fake_user(user_id, "solo@example.com", active_org_id=None)
    try:
        client = _client_with(storage, user)
        resp = client.get("/admin")
        assert resp.status_code == 403
        assert "organization" in resp.text.lower()
    finally:
        _cleanup()


@pytest.mark.parametrize("role", ["read_only", "staff", "clinician", "coordinator"])
def test_admin_overview_rejects_sub_admin_roles(storage: Storage, role: str) -> None:
    user_id = storage.create_user(f"{role}@example.com", "hashed")
    org = storage.create_organization(name="Acme Clinic", slug="acme")
    storage.create_membership(organization_id=org.id, user_id=user_id, role=role)
    storage.set_active_org(user_id, org.id)
    user = _fake_user(user_id, f"{role}@example.com", active_org_id=org.id, is_org_admin=False)
    try:
        client = _client_with(storage, user)
        resp = client.get("/admin")
        assert resp.status_code == 403
        assert "admin role" in resp.text.lower()
    finally:
        _cleanup()


@pytest.mark.parametrize("role", ["admin", "owner"])
def test_admin_overview_allows_admin_and_owner(storage: Storage, role: str) -> None:
    user_id = storage.create_user(f"{role}@example.com", "hashed")
    org = storage.create_organization(
        name="Cardiology Associates",
        slug=f"cardio-{role}",
        npi="1234567890",
    )
    storage.create_membership(organization_id=org.id, user_id=user_id, role=role)
    storage.set_active_org(user_id, org.id)
    user = _fake_user(
        user_id,
        f"{role}@example.com",
        active_org_id=org.id,
        is_org_admin=True,
    )
    try:
        client = _client_with(storage, user)
        resp = client.get("/admin")
        assert resp.status_code == 200
        assert "Cardiology Associates" in resp.text
        assert "Overview" in resp.text
    finally:
        _cleanup()


# --- Happy-path rendering ---


def test_admin_overview_renders_counts(storage: Storage) -> None:
    user_id = storage.create_user("admin@example.com", "hashed")
    org = storage.create_organization(name="North Clinic", slug="north")
    storage.create_membership(organization_id=org.id, user_id=user_id, role="owner")
    # Add a second member to exercise the member count.
    second_id = storage.create_user("m2@example.com", "hashed")
    storage.create_membership(organization_id=org.id, user_id=second_id, role="staff")
    storage.set_active_org(user_id, org.id)

    # Seed one platform-default specialty rule + one org override so both
    # count columns render non-zero.
    storage.create_specialty_rule(
        specialty_code="207RC0000X",
        organization_id=None,
        display_name="Cardiology (global)",
    )
    storage.create_specialty_rule(
        specialty_code="207RC0000X",
        organization_id=org.id,
        display_name="Cardiology (org override)",
        source="admin_override",
    )

    # One payer rule global, no override — exercises the "no overrides" hint.
    storage.create_payer_rule(
        payer_key="Medicare|medicare",
        display_name="Medicare",
    )

    user = _fake_user(
        user_id,
        "admin@example.com",
        active_org_id=org.id,
        is_org_admin=True,
    )
    try:
        client = _client_with(storage, user)
        resp = client.get("/admin")
        assert resp.status_code == 200
        body = resp.text
        # Active members = 2.
        assert "North Clinic" in body
        assert ">2<" in body  # member count rendered inside the stat-value div
        # Sidebar should highlight Overview and show "Coming soon" for others.
        assert 'class="admin-nav-link active"' in body or "admin-nav-link active" in body
        assert "Coming soon" in body
    finally:
        _cleanup()


def test_admin_overview_shows_empty_activity_when_no_events(storage: Storage) -> None:
    user_id = storage.create_user("admin@example.com", "hashed")
    org = storage.create_organization(name="Quiet Clinic", slug="quiet")
    storage.create_membership(organization_id=org.id, user_id=user_id, role="admin")
    storage.set_active_org(user_id, org.id)
    user = _fake_user(
        user_id,
        "admin@example.com",
        active_org_id=org.id,
        is_org_admin=True,
    )
    try:
        client = _client_with(storage, user)
        resp = client.get("/admin")
        assert resp.status_code == 200
        assert "No audit events" in resp.text
    finally:
        _cleanup()


def test_admin_overview_lists_recent_events(storage: Storage) -> None:
    user_id = storage.create_user("admin@example.com", "hashed")
    org = storage.create_organization(name="Noisy Clinic", slug="noisy")
    storage.create_membership(organization_id=org.id, user_id=user_id, role="admin")
    storage.set_active_org(user_id, org.id)
    storage.record_audit_event(
        action="patient.create",
        actor_user_id=user_id,
        scope_organization_id=org.id,
        entity_type="patient",
        entity_id="42",
    )
    user = _fake_user(
        user_id,
        "admin@example.com",
        active_org_id=org.id,
        is_org_admin=True,
    )
    try:
        client = _client_with(storage, user)
        resp = client.get("/admin")
        assert resp.status_code == 200
        assert "patient.create" in resp.text
        assert "#42" in resp.text
    finally:
        _cleanup()


# --- Nav link visibility (base.html) ---


def test_nav_shows_admin_link_when_user_is_org_admin(storage: Storage) -> None:
    user_id = storage.create_user("admin@example.com", "hashed")
    org = storage.create_organization(name="Visible Clinic", slug="visible")
    storage.create_membership(organization_id=org.id, user_id=user_id, role="admin")
    storage.set_active_org(user_id, org.id)
    user = _fake_user(
        user_id,
        "admin@example.com",
        active_org_id=org.id,
        is_org_admin=True,
    )
    try:
        client = _client_with(storage, user)
        # Any authenticated page renders base.html nav — use /profile as it's
        # always available for any logged-in user regardless of scope.
        resp = client.get("/profile")
        assert resp.status_code == 200
        assert 'href="/admin"' in resp.text
    finally:
        _cleanup()


def test_nav_hides_admin_link_for_non_admin(storage: Storage) -> None:
    user_id = storage.create_user("staff@example.com", "hashed")
    org = storage.create_organization(name="Hidden Clinic", slug="hidden")
    storage.create_membership(organization_id=org.id, user_id=user_id, role="staff")
    storage.set_active_org(user_id, org.id)
    user = _fake_user(
        user_id,
        "staff@example.com",
        active_org_id=org.id,
        is_org_admin=False,
    )
    try:
        client = _client_with(storage, user)
        resp = client.get("/profile")
        assert resp.status_code == 200
        # Specifically the navigation Admin link, not CSS classes containing
        # "admin"; anchor the assertion to the href.
        assert 'href="/admin"' not in resp.text
    finally:
        _cleanup()
