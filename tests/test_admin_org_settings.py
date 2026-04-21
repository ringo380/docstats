"""Route-level tests for admin org-settings editor (Phase 6.D)."""

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
    return Storage(db_path=tmp_path / "admin_org.db")


@pytest.fixture
def org_admin(storage: Storage):
    user_id = storage.create_user("admin@example.com", "hashed")
    org = storage.create_organization(
        name="Acme Clinic",
        slug="acme",
        npi="1111111111",
        address_line1="100 Original St",
        address_city="San Francisco",
        address_state="CA",
        address_zip="94110",
        phone="4155550001",
        fax="4155550002",
    )
    storage.create_membership(organization_id=org.id, user_id=user_id, role="admin")
    storage.set_active_org(user_id, org.id)
    user = _fake_user(user_id, "admin@example.com", active_org_id=org.id, is_org_admin=True)
    return user_id, org, user


def _client_with(storage: Storage, user: dict | None) -> TestClient:
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


def _cleanup() -> None:
    app.dependency_overrides.clear()


# --- Role enforcement ---


def test_rejects_solo_user(storage: Storage) -> None:
    user_id = storage.create_user("solo@example.com", "hashed")
    user = _fake_user(user_id, "solo@example.com", active_org_id=None)
    try:
        resp = _client_with(storage, user).get("/admin/org-settings")
        assert resp.status_code == 403
    finally:
        _cleanup()


@pytest.mark.parametrize("role", ["read_only", "staff", "clinician", "coordinator"])
def test_rejects_sub_admin(storage: Storage, role: str) -> None:
    user_id = storage.create_user(f"{role}@example.com", "hashed")
    org = storage.create_organization(name="R", slug=f"r-{role}")
    storage.create_membership(organization_id=org.id, user_id=user_id, role=role)
    storage.set_active_org(user_id, org.id)
    user = _fake_user(user_id, f"{role}@example.com", active_org_id=org.id, is_org_admin=False)
    try:
        resp = _client_with(storage, user).get("/admin/org-settings")
        assert resp.status_code == 403
    finally:
        _cleanup()


def test_save_rejects_non_admin(storage: Storage) -> None:
    user_id = storage.create_user("staff@example.com", "hashed")
    org = storage.create_organization(name="S", slug="s-org")
    storage.create_membership(organization_id=org.id, user_id=user_id, role="staff")
    storage.set_active_org(user_id, org.id)
    user = _fake_user(user_id, "staff@example.com", active_org_id=org.id, is_org_admin=False)
    try:
        resp = _client_with(storage, user).post("/admin/org-settings", data={"name": "Renamed"})
        assert resp.status_code == 403
    finally:
        _cleanup()


# --- Form GET ---


def test_form_pre_populates_from_existing_row(storage: Storage, org_admin) -> None:
    _, _, user = org_admin
    try:
        resp = _client_with(storage, user).get("/admin/org-settings")
        assert resp.status_code == 200
        body = resp.text
        assert 'value="Acme Clinic"' in body
        assert 'value="1111111111"' in body
        assert 'value="100 Original St"' in body
        assert 'value="San Francisco"' in body
        assert 'value="94110"' in body
        assert 'value="4155550001"' in body
    finally:
        _cleanup()


# --- Save happy path ---


def test_save_updates_all_fields(storage: Storage, org_admin) -> None:
    _, org, user = org_admin
    try:
        resp = _client_with(storage, user).post(
            "/admin/org-settings",
            data={
                "name": "Acme Cardiology",
                "npi": "2222222222",
                "address_line1": "200 New St",
                "address_line2": "Suite 300",
                "address_city": "Oakland",
                "address_state": "CA",
                "address_zip": "94612-1234",
                "phone": "5105550001",
                "fax": "5105550002",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/admin/org-settings"

        updated = storage.get_organization(org.id)
        assert updated is not None
        assert updated.name == "Acme Cardiology"
        assert updated.npi == "2222222222"
        assert updated.address_line1 == "200 New St"
        assert updated.address_line2 == "Suite 300"
        assert updated.address_city == "Oakland"
        assert updated.address_state == "CA"
        assert updated.address_zip == "94612-1234"
        assert updated.phone == "5105550001"
        assert updated.fax == "5105550002"
        # Slug is immutable via the settings form.
        assert updated.slug == "acme"

        events = storage.list_audit_events(scope_organization_id=org.id)
        assert any(e.action == "admin.org.update" for e in events)
    finally:
        _cleanup()


def test_save_clears_optional_fields_when_submitted_blank(storage: Storage, org_admin) -> None:
    """Regression: empty form inputs must write NULL (overwrite=True)."""
    _, org, user = org_admin
    try:
        resp = _client_with(storage, user).post(
            "/admin/org-settings",
            data={
                "name": "Acme Clinic",  # required, keep
                "npi": "",
                "address_line1": "",
                "address_line2": "",
                "address_city": "",
                "address_state": "",
                "address_zip": "",
                "phone": "",
                "fax": "",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        updated = storage.get_organization(org.id)
        assert updated is not None
        assert updated.npi is None
        assert updated.address_line1 is None
        assert updated.address_city is None
        assert updated.address_state is None
        assert updated.address_zip is None
        assert updated.phone is None
        assert updated.fax is None
    finally:
        _cleanup()


def test_save_hx_redirect(storage: Storage, org_admin) -> None:
    _, _, user = org_admin
    try:
        resp = _client_with(storage, user).post(
            "/admin/org-settings",
            data={"name": "Still Acme"},
            headers={"HX-Request": "true"},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert resp.headers.get("hx-redirect") == "/admin/org-settings"
    finally:
        _cleanup()


# --- Validation ---


def test_save_rejects_empty_name(storage: Storage, org_admin) -> None:
    """FastAPI Form(..., max_length=200) 422s on truly empty string, but
    whitespace-only falls to the route-level re-render."""
    _, org, user = org_admin
    try:
        resp = _client_with(storage, user).post(
            "/admin/org-settings",
            data={"name": "   "},
        )
        # Whitespace name → route-level error re-render (200) with message.
        assert resp.status_code == 200
        assert "Organization name is required" in resp.text
        # Row unchanged.
        unchanged = storage.get_organization(org.id)
        assert unchanged is not None
        assert unchanged.name == "Acme Clinic"
    finally:
        _cleanup()


def test_save_rejects_invalid_npi(storage: Storage, org_admin) -> None:
    _, org, user = org_admin
    try:
        resp = _client_with(storage, user).post(
            "/admin/org-settings",
            data={"name": "Acme Clinic", "npi": "abc"},
        )
        # FastAPI Form(max_length=10) accepts "abc", then route validate_npi rejects.
        assert resp.status_code == 200
        assert "NPI must be exactly 10 digits" in resp.text
        unchanged = storage.get_organization(org.id)
        assert unchanged is not None
        assert unchanged.npi == "1111111111"
    finally:
        _cleanup()


def test_save_rejects_unknown_state(storage: Storage, org_admin) -> None:
    _, _, user = org_admin
    try:
        resp = _client_with(storage, user).post(
            "/admin/org-settings",
            data={"name": "Acme Clinic", "address_state": "ZZ"},
        )
        assert resp.status_code == 200
        assert "Unknown state code" in resp.text
    finally:
        _cleanup()


def test_save_accepts_lowercase_state_and_upcases(storage: Storage, org_admin) -> None:
    _, org, user = org_admin
    try:
        resp = _client_with(storage, user).post(
            "/admin/org-settings",
            data={"name": "Acme Clinic", "address_state": "ny"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        updated = storage.get_organization(org.id)
        assert updated is not None
        assert updated.address_state == "NY"
    finally:
        _cleanup()


def test_save_rejects_invalid_zip(storage: Storage, org_admin) -> None:
    _, _, user = org_admin
    try:
        resp = _client_with(storage, user).post(
            "/admin/org-settings",
            data={"name": "Acme Clinic", "address_zip": "abc"},
        )
        assert resp.status_code == 200
        assert "ZIP" in resp.text
    finally:
        _cleanup()


@pytest.mark.parametrize("zip_code", ["94110", "94110-1234", "941101234"])
def test_save_accepts_valid_zip_formats(storage: Storage, org_admin, zip_code: str) -> None:
    _, org, user = org_admin
    try:
        resp = _client_with(storage, user).post(
            "/admin/org-settings",
            data={"name": "Acme Clinic", "address_zip": zip_code},
            follow_redirects=False,
        )
        assert resp.status_code == 303, f"zip={zip_code!r} should be accepted"
        updated = storage.get_organization(org.id)
        assert updated is not None
        assert updated.address_zip == zip_code
    finally:
        _cleanup()


def test_save_preserves_user_input_on_validation_error(storage: Storage, org_admin) -> None:
    """Admin shouldn't lose typing when the form rejects — re-render with
    submitted values, not the stored row."""
    _, _, user = org_admin
    try:
        resp = _client_with(storage, user).post(
            "/admin/org-settings",
            data={
                "name": "New name the admin typed",
                "npi": "not-digits",  # 10 chars, passes Form max_length, fails validate_npi
                "address_city": "New City",
            },
        )
        assert resp.status_code == 200
        body = resp.text
        assert 'value="New name the admin typed"' in body
        assert 'value="not-digits"' in body
        assert 'value="New City"' in body
    finally:
        _cleanup()
