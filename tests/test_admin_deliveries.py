"""Phase 9.E — Admin delivery console + storage + sweeper hardening tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from docstats.auth import get_current_user
from docstats.delivery.dispatcher import (
    _backoff_seconds,
    _BACKOFF_SECONDS,
    _should_skip_for_backoff,
    get_sweep_stats,
)
from docstats.domain.deliveries import Delivery
from docstats.scope import Scope
from docstats.storage import Storage, get_storage
from docstats.web import app


# ---------- Fixtures ----------


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
    return Storage(db_path=tmp_path / "admin_deliveries.db")


@pytest.fixture
def org_admin(storage: Storage):
    user_id = storage.create_user("admin@example.com", "hashed")
    org = storage.create_organization(name="Acme Clinic", slug="acme")
    storage.create_membership(organization_id=org.id, user_id=user_id, role="admin")
    storage.set_active_org(user_id, org.id)
    user = _fake_user(user_id, "admin@example.com", active_org_id=org.id, is_org_admin=True)
    return user_id, org, user


def _seed_delivery(storage: Storage, user_id: int, org_id: int, **kwargs) -> Delivery:
    scope = Scope(organization_id=org_id, membership_role="admin")
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
        reason="Consult",
        urgency="routine",
        specialty_desc="Cardiology",
        receiving_organization_name="Heart",
        created_by_user_id=user_id,
    )
    return storage.create_delivery(
        scope,
        referral_id=referral.id,
        channel=kwargs.get("channel", "fax"),
        recipient=kwargs.get("recipient", "+15555551234"),
    )


def _client_with(storage: Storage, user: dict | None) -> TestClient:
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


def _cleanup() -> None:
    app.dependency_overrides.clear()


# ---------- Role enforcement ----------


def test_rejects_solo_user(storage: Storage) -> None:
    user_id = storage.create_user("solo@example.com", "hashed")
    user = _fake_user(user_id, "solo@example.com", active_org_id=None)
    try:
        resp = _client_with(storage, user).get("/admin/deliveries")
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
        resp = _client_with(storage, user).get("/admin/deliveries")
        assert resp.status_code == 403
    finally:
        _cleanup()


# ---------- Storage: list_deliveries_for_admin ----------


def test_list_deliveries_for_admin_empty(storage: Storage, org_admin) -> None:
    _, org, _ = org_admin
    rows = storage.list_deliveries_for_admin(scope_organization_id=org.id)
    assert rows == []


def test_list_deliveries_for_admin_newest_first(storage: Storage, org_admin) -> None:
    user_id, org, _ = org_admin
    d1 = _seed_delivery(storage, user_id, org.id)
    d2 = _seed_delivery(storage, user_id, org.id, channel="email", recipient="x@y.com")
    rows = storage.list_deliveries_for_admin(scope_organization_id=org.id)
    assert [r.id for r in rows] == [d2.id, d1.id]


def test_list_deliveries_for_admin_channel_filter(storage: Storage, org_admin) -> None:
    user_id, org, _ = org_admin
    _seed_delivery(storage, user_id, org.id, channel="fax")
    d2 = _seed_delivery(storage, user_id, org.id, channel="email", recipient="a@b.com")
    rows = storage.list_deliveries_for_admin(scope_organization_id=org.id, channel="email")
    assert [r.id for r in rows] == [d2.id]


def test_list_deliveries_for_admin_status_filter(storage: Storage, org_admin) -> None:
    user_id, org, _ = org_admin
    d1 = _seed_delivery(storage, user_id, org.id)
    d2 = _seed_delivery(storage, user_id, org.id)
    storage.mark_delivery_sent(d1.id, vendor_name="Documo", vendor_message_id="m1")
    rows = storage.list_deliveries_for_admin(scope_organization_id=org.id, status="queued")
    assert [r.id for r in rows] == [d2.id]


def test_list_deliveries_for_admin_referral_filter(storage: Storage, org_admin) -> None:
    user_id, org, _ = org_admin
    d1 = _seed_delivery(storage, user_id, org.id)
    d2 = _seed_delivery(storage, user_id, org.id)
    # Both rows share referrals from _seed_delivery (separate referrals each).
    rows = storage.list_deliveries_for_admin(
        scope_organization_id=org.id, referral_id=d1.referral_id
    )
    assert [r.id for r in rows] == [d1.id]
    assert d2.id not in [r.id for r in rows]


def test_list_deliveries_for_admin_scope_isolation(storage: Storage, org_admin) -> None:
    """Org A's admin cannot see org B's deliveries."""
    user_id, org_a, _ = org_admin
    d_a = _seed_delivery(storage, user_id, org_a.id)

    # Build a sibling org with its own delivery.
    user_b = storage.create_user("b@example.com", "hashed")
    org_b = storage.create_organization(name="B", slug="b")
    storage.create_membership(organization_id=org_b.id, user_id=user_b, role="admin")
    d_b = _seed_delivery(storage, user_b, org_b.id)

    rows_a = storage.list_deliveries_for_admin(scope_organization_id=org_a.id)
    rows_b = storage.list_deliveries_for_admin(scope_organization_id=org_b.id)
    assert [r.id for r in rows_a] == [d_a.id]
    assert [r.id for r in rows_b] == [d_b.id]


def test_list_deliveries_for_admin_pagination(storage: Storage, org_admin) -> None:
    user_id, org, _ = org_admin
    ids = [_seed_delivery(storage, user_id, org.id).id for _ in range(5)]
    page1 = storage.list_deliveries_for_admin(scope_organization_id=org.id, limit=2, offset=0)
    page2 = storage.list_deliveries_for_admin(scope_organization_id=org.id, limit=2, offset=2)
    assert len(page1) == 2
    assert len(page2) == 2
    # Newest-first: the last-created id shows up in page1.
    assert page1[0].id == ids[-1]


# ---------- Storage: get_delivery_queue_stats ----------


def test_queue_stats_empty(storage: Storage, org_admin) -> None:
    _, org, _ = org_admin
    stats = storage.get_delivery_queue_stats(scope_organization_id=org.id)
    assert stats.queued == 0
    assert stats.oldest_queued_age_seconds is None


def test_queue_stats_counts_by_status(storage: Storage, org_admin) -> None:
    user_id, org, _ = org_admin
    d1 = _seed_delivery(storage, user_id, org.id)
    _seed_delivery(storage, user_id, org.id)
    d3 = _seed_delivery(storage, user_id, org.id)
    storage.mark_delivery_sent(d1.id, vendor_name="X", vendor_message_id="m1")
    storage.mark_delivery_failed(d3.id, error_code="x", error_message="y")

    stats = storage.get_delivery_queue_stats(scope_organization_id=org.id)
    assert stats.queued == 1
    assert stats.sent == 1
    assert stats.failed == 1


def test_queue_stats_oldest_queued_age_is_int(storage: Storage, org_admin) -> None:
    user_id, org, _ = org_admin
    _seed_delivery(storage, user_id, org.id)
    stats = storage.get_delivery_queue_stats(scope_organization_id=org.id)
    # Fresh row — age should be ≥ 0, not None
    assert stats.oldest_queued_age_seconds is not None
    assert stats.oldest_queued_age_seconds >= 0


def test_queue_stats_scope_isolation(storage: Storage, org_admin) -> None:
    user_id, org_a, _ = org_admin
    _seed_delivery(storage, user_id, org_a.id)

    user_b = storage.create_user("b@example.com", "hashed")
    org_b = storage.create_organization(name="B", slug="b")
    storage.create_membership(organization_id=org_b.id, user_id=user_b, role="admin")
    _seed_delivery(storage, user_b, org_b.id)

    stats_a = storage.get_delivery_queue_stats(scope_organization_id=org_a.id)
    stats_b = storage.get_delivery_queue_stats(scope_organization_id=org_b.id)
    assert stats_a.queued == 1
    assert stats_b.queued == 1


# ---------- Routes: list ----------


def test_list_renders_empty(storage: Storage, org_admin) -> None:
    _, _, user = org_admin
    try:
        resp = _client_with(storage, user).get("/admin/deliveries")
        assert resp.status_code == 200
        assert "No deliveries match" in resp.text
    finally:
        _cleanup()


def test_list_renders_rows(storage: Storage, org_admin) -> None:
    user_id, org, user = org_admin
    _seed_delivery(storage, user_id, org.id)
    try:
        resp = _client_with(storage, user).get("/admin/deliveries")
        assert resp.status_code == 200
        assert "+15555551234" in resp.text
        assert "queued" in resp.text
    finally:
        _cleanup()


def test_list_rejects_unknown_channel(storage: Storage, org_admin) -> None:
    _, _, user = org_admin
    try:
        resp = _client_with(storage, user).get("/admin/deliveries?channel=pigeon")
        assert resp.status_code == 422
    finally:
        _cleanup()


def test_list_rejects_unknown_status(storage: Storage, org_admin) -> None:
    _, _, user = org_admin
    try:
        resp = _client_with(storage, user).get("/admin/deliveries?status=vibing")
        assert resp.status_code == 422
    finally:
        _cleanup()


def test_list_accepts_blank_filters(storage: Storage, org_admin) -> None:
    """Bookmarked URLs with empty-string filters should render, not 422."""
    _, _, user = org_admin
    try:
        resp = _client_with(storage, user).get(
            "/admin/deliveries?channel=&status=&referral_id=&since=&until="
        )
        assert resp.status_code == 200
    finally:
        _cleanup()


def test_list_rejects_bad_date(storage: Storage, org_admin) -> None:
    _, _, user = org_admin
    try:
        resp = _client_with(storage, user).get("/admin/deliveries?since=yesterday")
        assert resp.status_code == 422
    finally:
        _cleanup()


# ---------- Routes: detail ----------


def test_detail_404_on_missing(storage: Storage, org_admin) -> None:
    _, _, user = org_admin
    try:
        resp = _client_with(storage, user).get("/admin/deliveries/999999")
        assert resp.status_code == 404
    finally:
        _cleanup()


def test_detail_renders(storage: Storage, org_admin) -> None:
    user_id, org, user = org_admin
    d = _seed_delivery(storage, user_id, org.id)
    try:
        resp = _client_with(storage, user).get(f"/admin/deliveries/{d.id}")
        assert resp.status_code == 200
        assert f"Delivery #{d.id}" in resp.text
        assert "+15555551234" in resp.text
    finally:
        _cleanup()


def test_detail_scope_isolation(storage: Storage, org_admin) -> None:
    """Admin of org A cannot fetch org B's delivery detail."""
    user_id, _, user_a = org_admin
    user_b = storage.create_user("b@example.com", "hashed")
    org_b = storage.create_organization(name="B", slug="b")
    storage.create_membership(organization_id=org_b.id, user_id=user_b, role="admin")
    d_b = _seed_delivery(storage, user_b, org_b.id)
    try:
        resp = _client_with(storage, user_a).get(f"/admin/deliveries/{d_b.id}")
        assert resp.status_code == 404
    finally:
        _cleanup()


# ---------- Routes: cancel ----------


def test_cancel_happy_path(storage: Storage, org_admin) -> None:
    user_id, org, user = org_admin
    d = _seed_delivery(storage, user_id, org.id)
    try:
        resp = _client_with(storage, user).post(
            f"/admin/deliveries/{d.id}/cancel", follow_redirects=False
        )
        assert resp.status_code == 303
        refreshed = storage.get_delivery(Scope(organization_id=org.id), d.id)
        assert refreshed is not None
        assert refreshed.status == "cancelled"
    finally:
        _cleanup()


def test_cancel_records_admin_audit(storage: Storage, org_admin) -> None:
    user_id, org, user = org_admin
    d = _seed_delivery(storage, user_id, org.id)
    try:
        _client_with(storage, user).post(f"/admin/deliveries/{d.id}/cancel", follow_redirects=False)
        events = storage.list_audit_events(
            scope_organization_id=org.id, action="admin.delivery.cancel"
        )
        assert len(events) == 1
        assert events[0].entity_id == str(d.id)
    finally:
        _cleanup()


def test_cancel_404_on_cross_scope(storage: Storage, org_admin) -> None:
    user_id, _, user_a = org_admin
    user_b = storage.create_user("b@example.com", "hashed")
    org_b = storage.create_organization(name="B", slug="b")
    storage.create_membership(organization_id=org_b.id, user_id=user_b, role="admin")
    d_b = _seed_delivery(storage, user_b, org_b.id)
    try:
        resp = _client_with(storage, user_a).post(
            f"/admin/deliveries/{d_b.id}/cancel", follow_redirects=False
        )
        assert resp.status_code == 404
    finally:
        _cleanup()


def test_cancel_htmx_returns_hx_redirect(storage: Storage, org_admin) -> None:
    user_id, org, user = org_admin
    d = _seed_delivery(storage, user_id, org.id)
    try:
        resp = _client_with(storage, user).post(
            f"/admin/deliveries/{d.id}/cancel",
            headers={"HX-Request": "true"},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert resp.headers.get("HX-Redirect") == f"/admin/deliveries/{d.id}"
    finally:
        _cleanup()


# ---------- Routes: health ----------


def test_health_html_renders(storage: Storage, org_admin) -> None:
    _, _, user = org_admin
    try:
        resp = _client_with(storage, user).get("/admin/deliveries/health")
        assert resp.status_code == 200
        assert "Queue depth" in resp.text
    finally:
        _cleanup()


def test_health_json_shape(storage: Storage, org_admin) -> None:
    user_id, org, user = org_admin
    _seed_delivery(storage, user_id, org.id)
    try:
        resp = _client_with(storage, user).get("/admin/deliveries/health.json")
        assert resp.status_code == 200
        data = resp.json()
        assert "queue" in data
        assert "sweeper" in data
        assert data["queue"]["queued"] == 1
        assert "running" in data["sweeper"]
    finally:
        _cleanup()


# ---------- Sweeper hardening: exponential backoff ----------


def test_backoff_first_retry_uses_first_step() -> None:
    """retry_count=1 → the first schedule entry (10s) ±15%."""
    vals = {_backoff_seconds(1) for _ in range(50)}
    assert all(8 <= v <= 12 for v in vals), vals


def test_backoff_zero_is_immediate() -> None:
    assert _backoff_seconds(0) == 0


def test_backoff_clamps_at_schedule_end() -> None:
    """Beyond the schedule we clamp to the final step (1h)."""
    final = _BACKOFF_SECONDS[-1]
    for _ in range(20):
        v = _backoff_seconds(len(_BACKOFF_SECONDS) + 3)
        assert abs(v - final) <= int(final * 0.15) + 1


def test_backoff_schedule_is_monotonic() -> None:
    assert list(_BACKOFF_SECONDS) == sorted(_BACKOFF_SECONDS)


def test_should_skip_for_backoff_fresh_row() -> None:
    """retry_count=0 never skips — first try is immediate."""
    d = _make_delivery(retry_count=0, updated_at=datetime.now(tz=timezone.utc))
    assert _should_skip_for_backoff(d) is False


def test_should_skip_for_backoff_recent_retry() -> None:
    """retry_count=1 with just-bumped updated_at → skip (within 10s window)."""
    d = _make_delivery(retry_count=1, updated_at=datetime.now(tz=timezone.utc))
    assert _should_skip_for_backoff(d) is True


def test_should_skip_for_backoff_elapsed() -> None:
    """retry_count=1 with updated_at 60s ago → don't skip (well past 10s)."""
    d = _make_delivery(
        retry_count=1,
        updated_at=datetime.now(tz=timezone.utc) - timedelta(seconds=60),
    )
    assert _should_skip_for_backoff(d) is False


def test_should_skip_for_backoff_naive_datetime() -> None:
    """Naive updated_at must be treated as UTC (not crash)."""
    naive_dt = (datetime.now(tz=timezone.utc) - timedelta(seconds=60)).replace(tzinfo=None)
    d = _make_delivery(retry_count=1, updated_at=naive_dt)
    # Should not raise; returns a bool.
    result = _should_skip_for_backoff(d)
    assert isinstance(result, bool)


def _make_delivery(*, retry_count: int, updated_at: datetime) -> Delivery:
    return Delivery(
        id=1,
        referral_id=1,
        channel="fax",
        recipient="+15555550000",
        status="queued",
        retry_count=retry_count,
        created_at=updated_at,
        updated_at=updated_at,
        packet_artifact={},
    )


# ---------- Sweeper hardening: stats ----------


def test_get_sweep_stats_returns_copy() -> None:
    """Mutating the returned object shouldn't affect internal state."""
    s1 = get_sweep_stats()
    s1.total_iterations = 999
    s2 = get_sweep_stats()
    assert s2.total_iterations != 999


def test_get_sweep_stats_exposed_fields() -> None:
    s = get_sweep_stats()
    # Field availability guards the /health.json contract.
    assert hasattr(s, "last_sweep_at")
    assert hasattr(s, "total_iterations")
    assert hasattr(s, "running")
    assert hasattr(s, "interval_seconds")


def test_stuck_sending_seconds_env_var(monkeypatch) -> None:
    from docstats.delivery.dispatcher import _get_stuck_sending_seconds

    monkeypatch.setenv("DELIVERY_STUCK_SENDING_SECONDS", "300")
    assert _get_stuck_sending_seconds() == 300
    monkeypatch.setenv("DELIVERY_STUCK_SENDING_SECONDS", "10")
    # Clamped to min 30
    assert _get_stuck_sending_seconds() == 30
    monkeypatch.setenv("DELIVERY_STUCK_SENDING_SECONDS", "99999")
    # Clamped to max 3600
    assert _get_stuck_sending_seconds() == 3600
