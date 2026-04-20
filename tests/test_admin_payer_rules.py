"""Route-level tests for admin payer-rules editor (Phase 6.C)."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

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
    return Storage(db_path=tmp_path / "admin_payer.db")


# Seeded payer_key uses pipe + spaces; URL-encoded it becomes
# "Medicare%7Cmedicare" (no spaces in this one). A key with spaces exercises
# the encoding path end-to-end.
SEED_KEY = "Medicare|medicare"
SEED_KEY_SPACES = "Blue Cross Blue Shield|ppo"


@pytest.fixture
def org_admin(storage: Storage):
    """Set up an org with a single admin user + two global payer rules."""
    user_id = storage.create_user("admin@example.com", "hashed")
    org = storage.create_organization(name="Acme Clinic", slug="acme")
    storage.create_membership(organization_id=org.id, user_id=user_id, role="admin")
    storage.set_active_org(user_id, org.id)
    # Seed two global rules — one no-auth, one with a services list — so the
    # list view exercises both render paths.
    storage.create_payer_rule(
        payer_key=SEED_KEY,
        display_name="Medicare (Original)",
        referral_required=False,
        auth_required_services={"services": ["home health"]},
        auth_typical_turnaround_days=None,
        records_required={"kinds": ["medical necessity"]},
        notes="Most specialist visits do not require prior auth.",
    )
    storage.create_payer_rule(
        payer_key=SEED_KEY_SPACES,
        display_name="Blue Cross Blue Shield PPO",
        referral_required=False,
        auth_required_services={"services": ["MRI", "PET"]},
        auth_typical_turnaround_days=5,
        records_required={"kinds": ["recent notes"]},
        notes="PPO; auth on high-cost imaging.",
    )
    user = _fake_user(user_id, "admin@example.com", active_org_id=org.id, is_org_admin=True)
    return user_id, org, user


def _client_with(storage: Storage, user: dict | None) -> TestClient:
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


def _cleanup() -> None:
    app.dependency_overrides.clear()


def _url(base: str, key: str) -> str:
    """URL-encode the payer key for path substitution.

    ``quote(key, safe='')`` encodes both ``|`` and spaces so the resulting
    URL round-trips through FastAPI's path parser back to the original key.
    """
    return f"{base}/{quote(key, safe='')}"


# --- Role enforcement ---


def test_list_rejects_solo_user(storage: Storage) -> None:
    user_id = storage.create_user("solo@example.com", "hashed")
    user = _fake_user(user_id, "solo@example.com", active_org_id=None)
    try:
        resp = _client_with(storage, user).get("/admin/payer-rules")
        assert resp.status_code == 403
    finally:
        _cleanup()


@pytest.mark.parametrize("role", ["read_only", "staff", "clinician", "coordinator"])
def test_list_rejects_sub_admin(storage: Storage, role: str) -> None:
    user_id = storage.create_user(f"{role}@example.com", "hashed")
    org = storage.create_organization(name="R", slug=f"r-{role}")
    storage.create_membership(organization_id=org.id, user_id=user_id, role=role)
    storage.set_active_org(user_id, org.id)
    user = _fake_user(user_id, f"{role}@example.com", active_org_id=org.id, is_org_admin=False)
    try:
        resp = _client_with(storage, user).get("/admin/payer-rules")
        assert resp.status_code == 403
    finally:
        _cleanup()


def test_edit_rejects_anonymous(storage: Storage) -> None:
    try:
        resp = _client_with(storage, None).get(
            _url("/admin/payer-rules", SEED_KEY), follow_redirects=False
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/auth/login"
    finally:
        _cleanup()


# --- List view ---


def test_list_shows_globals_with_no_override(storage: Storage, org_admin) -> None:
    _, _, user = org_admin
    try:
        resp = _client_with(storage, user).get("/admin/payer-rules")
        assert resp.status_code == 200
        body = resp.text
        assert "Medicare (Original)" in body
        assert "Blue Cross Blue Shield PPO" in body
        assert "Platform default" in body
        assert "Create override" in body
        assert "Revert" not in body
    finally:
        _cleanup()


def test_list_highlights_existing_override(storage: Storage, org_admin) -> None:
    _, org, user = org_admin
    storage.create_payer_rule(
        payer_key=SEED_KEY,
        organization_id=org.id,
        display_name="Medicare (org override)",
        referral_required=True,
        source="admin_override",
    )
    try:
        resp = _client_with(storage, user).get("/admin/payer-rules")
        assert resp.status_code == 200
        body = resp.text
        assert "Org override" in body
        assert "Medicare (org override)" in body
        assert "Edit override" in body
        assert "Revert" in body
    finally:
        _cleanup()


# --- Edit form GET ---


def test_edit_form_prepopulates_from_global(storage: Storage, org_admin) -> None:
    _, _, user = org_admin
    try:
        resp = _client_with(storage, user).get(_url("/admin/payer-rules", SEED_KEY))
        assert resp.status_code == 200
        body = resp.text
        assert 'value="Medicare (Original)"' in body
        assert "home health" in body
        assert "medical necessity" in body
        # referral_required is False — checkbox must NOT be checked.
        assert 'name="referral_required" value="on"' in body
        assert "checked" not in body.split('name="referral_required"')[0].split("<input")[-1]
        assert "Create override" in body
    finally:
        _cleanup()


def test_edit_form_handles_payer_key_with_spaces(storage: Storage, org_admin) -> None:
    """URL-encoded spaces in the path round-trip back to the original key."""
    _, _, user = org_admin
    try:
        resp = _client_with(storage, user).get(_url("/admin/payer-rules", SEED_KEY_SPACES))
        assert resp.status_code == 200
        assert "Blue Cross Blue Shield PPO" in resp.text
    finally:
        _cleanup()


def test_edit_form_prepopulates_from_override_when_present(storage: Storage, org_admin) -> None:
    _, org, user = org_admin
    storage.create_payer_rule(
        payer_key=SEED_KEY,
        organization_id=org.id,
        display_name="Medicare (strict)",
        referral_required=True,
        auth_required_services={"services": ["override-service"]},
        auth_typical_turnaround_days=7,
        records_required={"kinds": ["override-record"]},
        notes="Org-specific Medicare handling",
        source="admin_override",
    )
    try:
        resp = _client_with(storage, user).get(_url("/admin/payer-rules", SEED_KEY))
        assert resp.status_code == 200
        body = resp.text
        assert "Medicare (strict)" in body
        assert "override-service" in body
        assert 'value="7"' in body
        assert "override-record" in body
        assert "Org-specific Medicare handling" in body
        # Global-only value should not leak into the form.
        assert "home health" not in body
        assert "Save override" in body
    finally:
        _cleanup()


def test_edit_form_404_when_unknown_key(storage: Storage, org_admin) -> None:
    _, _, user = org_admin
    try:
        resp = _client_with(storage, user).get(_url("/admin/payer-rules", "Bogus|plan"))
        assert resp.status_code == 404
    finally:
        _cleanup()


# --- Save (create override) ---


def test_save_creates_org_override(storage: Storage, org_admin) -> None:
    _, org, user = org_admin
    try:
        resp = _client_with(storage, user).post(
            _url("/admin/payer-rules", SEED_KEY),
            data={
                "display_name": "Medicare — team override",
                "referral_required": "on",
                "auth_required_services": "MRI\nPET",
                "auth_typical_turnaround_days": "5",
                "records_required": "notes\nimaging",
                "notes": "Updated policy 2026-04-20",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/admin/payer-rules"

        overrides = storage.list_payer_rules(
            organization_id=org.id, include_globals=False, payer_key=SEED_KEY
        )
        assert len(overrides) == 1
        o = overrides[0]
        assert o.display_name == "Medicare — team override"
        assert o.referral_required is True
        assert o.auth_required_services == {"services": ["MRI", "PET"]}
        assert o.auth_typical_turnaround_days == 5
        assert o.records_required == {"kinds": ["notes", "imaging"]}
        assert o.notes == "Updated policy 2026-04-20"
        assert o.source == "admin_override"

        events = storage.list_audit_events(scope_organization_id=org.id)
        assert any(e.action == "admin.payer_rule.create_override" for e in events)
    finally:
        _cleanup()


def test_save_treats_missing_checkbox_as_false(storage: Storage, org_admin) -> None:
    """HTML checkboxes omit the field entirely when unchecked — the Form
    default ``off`` must be interpreted as ``referral_required=False``."""
    _, org, user = org_admin
    try:
        resp = _client_with(storage, user).post(
            _url("/admin/payer-rules", SEED_KEY),
            data={"display_name": "No-referral override"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        override = storage.list_payer_rules(
            organization_id=org.id, include_globals=False, payer_key=SEED_KEY
        )[0]
        assert override.referral_required is False
    finally:
        _cleanup()


def test_save_rejects_invalid_turnaround(storage: Storage, org_admin) -> None:
    _, _, user = org_admin
    try:
        resp = _client_with(storage, user).post(
            _url("/admin/payer-rules", SEED_KEY),
            data={
                "display_name": "X",
                "auth_typical_turnaround_days": "not-a-number",
            },
        )
        assert resp.status_code == 422
    finally:
        _cleanup()


def test_save_rejects_out_of_range_turnaround(storage: Storage, org_admin) -> None:
    _, _, user = org_admin
    try:
        resp = _client_with(storage, user).post(
            _url("/admin/payer-rules", SEED_KEY),
            data={
                "display_name": "X",
                "auth_typical_turnaround_days": "999",
            },
        )
        assert resp.status_code == 422
    finally:
        _cleanup()


def test_save_accepts_blank_turnaround(storage: Storage, org_admin) -> None:
    """Blank turnaround → None, not an error."""
    _, org, user = org_admin
    try:
        resp = _client_with(storage, user).post(
            _url("/admin/payer-rules", SEED_KEY),
            data={
                "display_name": "No-turnaround override",
                "auth_typical_turnaround_days": "",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        override = storage.list_payer_rules(
            organization_id=org.id, include_globals=False, payer_key=SEED_KEY
        )[0]
        assert override.auth_typical_turnaround_days is None
    finally:
        _cleanup()


def test_save_updates_existing_override(storage: Storage, org_admin) -> None:
    _, org, user = org_admin
    before = storage.create_payer_rule(
        payer_key=SEED_KEY,
        organization_id=org.id,
        display_name="Old override",
        referral_required=False,
        auth_typical_turnaround_days=3,
        source="admin_override",
    )
    try:
        resp = _client_with(storage, user).post(
            _url("/admin/payer-rules", SEED_KEY),
            data={
                "display_name": "New name",
                "referral_required": "on",
                "auth_required_services": "service",
                "auth_typical_turnaround_days": "10",
                "records_required": "",
                "notes": "",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        after = storage.get_payer_rule(before.id)
        assert after is not None
        assert after.display_name == "New name"
        assert after.referral_required is True
        assert after.auth_typical_turnaround_days == 10
        assert after.records_required == {"kinds": []}
        assert after.version_id > before.version_id

        events = storage.list_audit_events(scope_organization_id=org.id)
        assert any(e.action == "admin.payer_rule.update_override" for e in events)
    finally:
        _cleanup()


def test_update_override_clears_display_name_when_field_emptied(
    storage: Storage, org_admin
) -> None:
    """Regression: empty display_name form field must clear the stored value,
    not silently preserve it (overwrite=True on update_payer_rule)."""
    _, org, user = org_admin
    storage.create_payer_rule(
        payer_key=SEED_KEY,
        organization_id=org.id,
        display_name="Old name",
        source="admin_override",
    )
    try:
        resp = _client_with(storage, user).post(
            _url("/admin/payer-rules", SEED_KEY),
            data={"display_name": ""},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        override = storage.list_payer_rules(
            organization_id=org.id, include_globals=False, payer_key=SEED_KEY
        )[0]
        assert override.display_name is None, (
            "Cleared display_name must be written through; default overwrite=False "
            "would silently preserve the old value."
        )
    finally:
        _cleanup()


def test_save_404_on_unknown_key(storage: Storage, org_admin) -> None:
    _, _, user = org_admin
    try:
        resp = _client_with(storage, user).post(
            _url("/admin/payer-rules", "Bogus|plan"),
            data={"display_name": "X"},
        )
        assert resp.status_code == 404
    finally:
        _cleanup()


# --- Revert ---


def test_revert_deletes_override(storage: Storage, org_admin) -> None:
    _, org, user = org_admin
    storage.create_payer_rule(
        payer_key=SEED_KEY,
        organization_id=org.id,
        display_name="To revert",
        source="admin_override",
    )
    try:
        resp = _client_with(storage, user).post(
            _url("/admin/payer-rules", SEED_KEY) + "/revert",
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert (
            len(
                storage.list_payer_rules(
                    organization_id=org.id, include_globals=False, payer_key=SEED_KEY
                )
            )
            == 0
        )
        # Global still present.
        globals_ = storage.list_payer_rules(
            organization_id=None, include_globals=True, payer_key=SEED_KEY
        )
        assert len(globals_) == 1

        events = storage.list_audit_events(scope_organization_id=org.id)
        assert any(e.action == "admin.payer_rule.revert_override" for e in events)
    finally:
        _cleanup()


def test_revert_is_idempotent_when_no_override(storage: Storage, org_admin) -> None:
    _, org, user = org_admin
    try:
        resp = _client_with(storage, user).post(
            _url("/admin/payer-rules", SEED_KEY) + "/revert",
            follow_redirects=False,
        )
        assert resp.status_code == 303
        events = storage.list_audit_events(scope_organization_id=org.id)
        assert all(e.action != "admin.payer_rule.revert_override" for e in events)
    finally:
        _cleanup()


# --- HX-Request discipline ---


def test_save_hx_redirect(storage: Storage, org_admin) -> None:
    _, _, user = org_admin
    try:
        resp = _client_with(storage, user).post(
            _url("/admin/payer-rules", SEED_KEY),
            data={"display_name": "OK"},
            headers={"HX-Request": "true"},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert resp.headers.get("hx-redirect") == "/admin/payer-rules"
    finally:
        _cleanup()


def test_revert_hx_redirect(storage: Storage, org_admin) -> None:
    _, org, user = org_admin
    storage.create_payer_rule(
        payer_key=SEED_KEY,
        organization_id=org.id,
        display_name="To revert",
        source="admin_override",
    )
    try:
        resp = _client_with(storage, user).post(
            _url("/admin/payer-rules", SEED_KEY) + "/revert",
            headers={"HX-Request": "true"},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert resp.headers.get("hx-redirect") == "/admin/payer-rules"
    finally:
        _cleanup()
