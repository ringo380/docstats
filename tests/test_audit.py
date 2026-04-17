"""Tests for the append-only audit log primitive."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from docstats.domain.audit import AuditEvent, client_ip, record
from docstats.storage import Storage


@pytest.fixture
def user_id(storage: Storage) -> int:
    return storage.create_user("audit@example.com", "hashed")


# --- SQLite storage layer ---


def test_record_and_list_simple(storage: Storage, user_id: int) -> None:
    row_id = storage.record_audit_event(
        action="user.login",
        actor_user_id=user_id,
        scope_user_id=user_id,
        ip="203.0.113.7",
        user_agent="pytest",
    )
    assert row_id > 0

    events = storage.list_audit_events(actor_user_id=user_id)
    assert len(events) == 1
    event = events[0]
    assert isinstance(event, AuditEvent)
    assert event.action == "user.login"
    assert event.actor_user_id == user_id
    assert event.scope_user_id == user_id
    assert event.scope_organization_id is None
    assert event.ip == "203.0.113.7"
    assert event.user_agent == "pytest"
    assert event.metadata == {}


def test_record_persists_metadata_as_json(storage: Storage, user_id: int) -> None:
    storage.record_audit_event(
        action="provider.save",
        actor_user_id=user_id,
        scope_user_id=user_id,
        entity_type="provider",
        entity_id="1234567890",
        metadata={"specialty": "Cardiology", "nested": {"k": 1}},
    )

    events = storage.list_audit_events(entity_type="provider", entity_id="1234567890")
    assert len(events) == 1
    assert events[0].metadata == {"specialty": "Cardiology", "nested": {"k": 1}}


def test_list_filters_compose(storage: Storage, user_id: int) -> None:
    other = storage.create_user("other@example.com", "hashed")
    storage.record_audit_event(action="user.login", actor_user_id=user_id, scope_user_id=user_id)
    storage.record_audit_event(action="user.login", actor_user_id=other, scope_user_id=other)
    storage.record_audit_event(
        action="provider.save",
        actor_user_id=user_id,
        scope_user_id=user_id,
        entity_type="provider",
        entity_id="npi-1",
    )

    all_mine = storage.list_audit_events(actor_user_id=user_id)
    assert len(all_mine) == 2
    assert {e.action for e in all_mine} == {"user.login", "provider.save"}

    only_saves = storage.list_audit_events(actor_user_id=user_id, entity_type="provider")
    assert len(only_saves) == 1
    assert only_saves[0].action == "provider.save"


def test_list_orders_newest_first(storage: Storage, user_id: int) -> None:
    storage.record_audit_event(action="user.login", actor_user_id=user_id)
    storage.record_audit_event(action="user.logout", actor_user_id=user_id)
    storage.record_audit_event(action="provider.save", actor_user_id=user_id)

    events = storage.list_audit_events(actor_user_id=user_id)
    # Same-second inserts mean id DESC is the deterministic tiebreaker.
    assert [e.action for e in events] == ["provider.save", "user.logout", "user.login"]


def test_list_limit(storage: Storage, user_id: int) -> None:
    for _ in range(10):
        storage.record_audit_event(action="user.login", actor_user_id=user_id)
    events = storage.list_audit_events(actor_user_id=user_id, limit=3)
    assert len(events) == 3


def test_deleted_user_nulls_actor_but_preserves_row(storage: Storage) -> None:
    """Audit rows must survive user deletion (ON DELETE SET NULL, not CASCADE)."""
    uid = storage.create_user("doomed@example.com", "hashed")
    storage.record_audit_event(action="user.signup", actor_user_id=uid, scope_user_id=uid)

    # Delete the user directly via SQL (no public delete_user API today).
    storage._conn.execute("DELETE FROM users WHERE id = ?", (uid,))
    storage._conn.commit()

    # Filtering by the deleted user_id returns nothing (column is now NULL)
    orphaned = storage.list_audit_events(actor_user_id=uid)
    assert orphaned == []
    # But the rows are still there when queried broadly
    row = storage._conn.execute(
        "SELECT actor_user_id, action FROM audit_events WHERE action = 'user.signup'"
    ).fetchone()
    assert row is not None
    assert row["actor_user_id"] is None
    assert row["action"] == "user.signup"


def test_actions_vocabulary_freely_extensible(storage: Storage, user_id: int) -> None:
    # Action is an open string; callers are responsible for using the documented verbs.
    storage.record_audit_event(action="referral.status_changed", actor_user_id=user_id)
    assert storage.list_audit_events(actor_user_id=user_id)[0].action == "referral.status_changed"


# --- record() helper ---


def _fake_request(
    *, xff: str | None = None, ua: str | None = None, client_host: str | None = None
) -> MagicMock:
    req = MagicMock()
    req.headers = {}
    if xff is not None:
        req.headers["X-Forwarded-For"] = xff
    if ua is not None:
        req.headers["User-Agent"] = ua
    if client_host is None:
        req.client = None
    else:
        req.client = MagicMock(host=client_host)
    return req


def test_client_ip_prefers_xff_leftmost() -> None:
    req = _fake_request(xff="203.0.113.5, 10.0.0.1, 10.0.0.2", client_host="10.0.0.9")
    assert client_ip(req) == "203.0.113.5"


def test_client_ip_falls_back_to_client_host() -> None:
    req = _fake_request(client_host="192.0.2.1")
    assert client_ip(req) == "192.0.2.1"


def test_client_ip_handles_no_client() -> None:
    assert client_ip(_fake_request()) is None


def test_client_ip_ignores_empty_xff() -> None:
    req = _fake_request(xff="", client_host="192.0.2.2")
    assert client_ip(req) == "192.0.2.2"


def test_record_helper_fills_request_context(storage: Storage, user_id: int) -> None:
    req = _fake_request(xff="203.0.113.99", ua="pytest/1.0", client_host="10.0.0.1")
    record(
        storage,
        action="user.login",
        request=req,
        actor_user_id=user_id,
        scope_user_id=user_id,
    )
    event = storage.list_audit_events(actor_user_id=user_id)[0]
    assert event.ip == "203.0.113.99"
    assert event.user_agent == "pytest/1.0"


def test_record_helper_truncates_long_user_agent(storage: Storage, user_id: int) -> None:
    long_ua = "x" * 2000
    req = _fake_request(ua=long_ua, client_host="10.0.0.1")
    record(storage, action="user.login", request=req, actor_user_id=user_id)
    event = storage.list_audit_events(actor_user_id=user_id)[0]
    assert event.user_agent is not None
    assert len(event.user_agent) == 500


def test_record_helper_never_raises(storage: Storage, user_id: int) -> None:
    """Audit-log failures must not break the calling route."""
    broken_storage = MagicMock()
    broken_storage.record_audit_event.side_effect = RuntimeError("db offline")
    # Should not raise; error is logged and swallowed.
    record(broken_storage, action="user.login", actor_user_id=user_id)


def test_record_helper_without_request(storage: Storage, user_id: int) -> None:
    record(storage, action="cli.backfill", actor_user_id=user_id)
    event = storage.list_audit_events(actor_user_id=user_id)[0]
    assert event.action == "cli.backfill"
    assert event.ip is None
    assert event.user_agent is None


def test_audit_event_created_at_is_tz_aware(storage: Storage, user_id: int) -> None:
    """SQLite stores naive UTC timestamps; callers compare AuditEvent.created_at
    against datetime.now(tz=timezone.utc). The row-to-model helper must attach
    timezone.utc so those comparisons don't raise TypeError."""
    from datetime import datetime, timezone

    storage.record_audit_event(action="user.login", actor_user_id=user_id)
    event = storage.list_audit_events(actor_user_id=user_id)[0]
    assert event.created_at.tzinfo is not None
    # And a tz-aware comparison works without raising:
    assert event.created_at <= datetime.now(tz=timezone.utc)
