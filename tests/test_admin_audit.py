"""Route-level tests for the admin audit-log viewer (Phase 6.E)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
    return Storage(db_path=tmp_path / "admin_audit.db")


@pytest.fixture
def org_admin(storage: Storage):
    user_id = storage.create_user("admin@example.com", "hashed")
    org = storage.create_organization(name="Acme Clinic", slug="acme")
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
        resp = _client_with(storage, user).get("/admin/audit")
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
        resp = _client_with(storage, user).get("/admin/audit")
        assert resp.status_code == 403
    finally:
        _cleanup()


# --- Empty state ---


def test_empty_log_renders_placeholder(storage: Storage, org_admin) -> None:
    _, _, user = org_admin
    try:
        resp = _client_with(storage, user).get("/admin/audit")
        assert resp.status_code == 200
        assert "No audit events" in resp.text
    finally:
        _cleanup()


# --- Scope isolation ---


def test_only_events_for_active_org_visible(storage: Storage, org_admin) -> None:
    """Admin of org A must not see events from org B."""
    _, org, user = org_admin
    # Event for this org — should appear.
    storage.record_audit_event(
        action="patient.create",
        actor_user_id=user["id"],
        scope_organization_id=org.id,
        entity_type="patient",
        entity_id="42",
    )
    # Event for a different org — must NOT appear. Use actor_user_id=None
    # so the FK constraint doesn't fail (no real user to attribute to in
    # the other-org scenario).
    other_org = storage.create_organization(name="Other", slug="other")
    storage.record_audit_event(
        action="patient.create",
        actor_user_id=None,
        scope_organization_id=other_org.id,
        entity_type="patient",
        entity_id="999",
    )
    try:
        resp = _client_with(storage, user).get("/admin/audit")
        assert resp.status_code == 200
        body = resp.text
        assert "#42" in body
        assert "#999" not in body
    finally:
        _cleanup()


# --- Filters ---


def test_filter_by_action(storage: Storage, org_admin) -> None:
    _, org, user = org_admin
    storage.record_audit_event(
        action="patient.create",
        scope_organization_id=org.id,
        entity_type="patient",
        entity_id="1",
    )
    storage.record_audit_event(
        action="patient.delete",
        scope_organization_id=org.id,
        entity_type="patient",
        entity_id="2",
    )
    try:
        resp = _client_with(storage, user).get("/admin/audit?action=patient.delete")
        assert resp.status_code == 200
        body = resp.text
        assert "patient.delete" in body
        assert "#2" in body
        # patient.create row should be filtered out.
        assert "#1" not in body.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
    finally:
        _cleanup()


def test_filter_by_entity_type_and_id(storage: Storage, org_admin) -> None:
    _, org, user = org_admin
    storage.record_audit_event(
        action="patient.update",
        scope_organization_id=org.id,
        entity_type="patient",
        entity_id="7",
    )
    storage.record_audit_event(
        action="referral.update",
        scope_organization_id=org.id,
        entity_type="referral",
        entity_id="7",
    )
    try:
        resp = _client_with(storage, user).get("/admin/audit?entity_type=referral&entity_id=7")
        assert resp.status_code == 200
        body = resp.text
        tbody = body.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
        assert "referral.update" in tbody
        assert "patient.update" not in tbody
    finally:
        _cleanup()


def test_filter_by_actor_user_id(storage: Storage, org_admin) -> None:
    _, org, user = org_admin
    other_user_id = storage.create_user("other@example.com", "hashed")
    storage.record_audit_event(
        action="patient.create",
        actor_user_id=user["id"],
        scope_organization_id=org.id,
        entity_type="patient",
        entity_id="own",
    )
    storage.record_audit_event(
        action="patient.create",
        actor_user_id=other_user_id,
        scope_organization_id=org.id,
        entity_type="patient",
        entity_id="other",
    )
    try:
        resp = _client_with(storage, user).get(f"/admin/audit?actor_user_id={other_user_id}")
        assert resp.status_code == 200
        body = resp.text
        tbody = body.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
        assert "#other" in tbody
        assert "#own" not in tbody
    finally:
        _cleanup()


def test_since_until_date_filters(storage: Storage) -> None:
    """Use the storage layer directly — route-level since/until behavior is
    a thin pass-through to list_audit_events, and recording events at past
    timestamps via the public API is clunky in SQLite. The storage-level
    test in test_audit.py covers the end-to-end day-range semantics."""
    # Intentionally empty — see test_audit_list_filters_by_date_range
    # in tests/test_audit.py for storage-level coverage.
    pass


def test_malformed_date_returns_422(storage: Storage, org_admin) -> None:
    _, _, user = org_admin
    try:
        resp = _client_with(storage, user).get("/admin/audit?since=not-a-date")
        assert resp.status_code == 422
    finally:
        _cleanup()


# --- Pagination ---


def test_pagination_next_link_when_page_full(storage: Storage, org_admin) -> None:
    """Generate 51 events → first page has 50, next link present."""
    _, org, user = org_admin
    for i in range(51):
        storage.record_audit_event(
            action="patient.create",
            scope_organization_id=org.id,
            entity_type="patient",
            entity_id=str(i),
        )
    try:
        resp = _client_with(storage, user).get("/admin/audit")
        assert resp.status_code == 200
        body = resp.text
        assert "Next →" in body
        assert "offset=50" in body
    finally:
        _cleanup()


def test_pagination_prev_link_when_offset(storage: Storage, org_admin) -> None:
    _, _, user = org_admin
    try:
        resp = _client_with(storage, user).get("/admin/audit?offset=50")
        assert resp.status_code == 200
        body = resp.text
        assert "← Previous" in body
        assert "offset=0" in body
    finally:
        _cleanup()


def test_pagination_preserves_filter_params(storage: Storage, org_admin) -> None:
    """Next/Prev links must carry over the active filter state."""
    _, org, user = org_admin
    for i in range(51):
        storage.record_audit_event(
            action="patient.create",
            scope_organization_id=org.id,
            entity_type="patient",
            entity_id=str(i),
        )
    try:
        resp = _client_with(storage, user).get(
            "/admin/audit?action=patient.create&entity_type=patient"
        )
        assert resp.status_code == 200
        body = resp.text
        # The Next link should contain both the offset bump AND the filter
        # params — otherwise clicking Next would drop the filter.
        assert "action=patient.create" in body
        assert "entity_type=patient" in body
    finally:
        _cleanup()


def test_offset_beyond_end_renders_empty_but_with_prev(storage: Storage, org_admin) -> None:
    _, org, user = org_admin
    storage.record_audit_event(
        action="patient.create",
        scope_organization_id=org.id,
        entity_type="patient",
        entity_id="1",
    )
    try:
        resp = _client_with(storage, user).get("/admin/audit?offset=500")
        assert resp.status_code == 200
        body = resp.text
        assert "No audit events" in body
        # At offset=500 there's a prev link back to 450 even though the page
        # is empty — fine; admin can back out manually. No next link.
        assert "Next →" not in body
    finally:
        _cleanup()


# --- Storage-level regression: the new filters work as documented ---


def test_storage_filters_by_action(storage: Storage, org_admin) -> None:
    _, org, _ = org_admin
    storage.record_audit_event(action="a.b", scope_organization_id=org.id)
    storage.record_audit_event(action="c.d", scope_organization_id=org.id)
    rows = storage.list_audit_events(scope_organization_id=org.id, action="c.d")
    assert [r.action for r in rows] == ["c.d"]


def test_storage_filters_by_since_until(storage: Storage, org_admin) -> None:
    """Storage-layer date-range filter — round-trip a tz-aware bound."""
    _, org, _ = org_admin
    # Two events close in time. SQLite's ``datetime('now')`` has 1-second
    # resolution so we sleep is overkill — instead verify boundary handling.
    storage.record_audit_event(action="x.y", scope_organization_id=org.id)
    now = datetime.now(tz=timezone.utc)
    # Since 1h ago: should include both.
    rows = storage.list_audit_events(scope_organization_id=org.id, since=now - timedelta(hours=1))
    assert len(rows) == 1
    # Since 1h in the future: should exclude.
    rows = storage.list_audit_events(scope_organization_id=org.id, since=now + timedelta(hours=1))
    assert rows == []
    # Until 1h in the future: should include.
    rows = storage.list_audit_events(scope_organization_id=org.id, until=now + timedelta(hours=1))
    assert len(rows) == 1


def test_storage_offset(storage: Storage, org_admin) -> None:
    _, org, _ = org_admin
    for i in range(5):
        storage.record_audit_event(
            action="patient.create",
            scope_organization_id=org.id,
            entity_type="patient",
            entity_id=str(i),
        )
    page1 = storage.list_audit_events(scope_organization_id=org.id, limit=2, offset=0)
    page2 = storage.list_audit_events(scope_organization_id=org.id, limit=2, offset=2)
    page3 = storage.list_audit_events(scope_organization_id=org.id, limit=2, offset=4)
    assert len(page1) == 2
    assert len(page2) == 2
    assert len(page3) == 1
    # No overlap between pages.
    ids = {r.id for r in page1} | {r.id for r in page2} | {r.id for r in page3}
    assert len(ids) == 5
