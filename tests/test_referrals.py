"""Tests for referrals + referral_events + state machine (Phase 1.B)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from docstats.domain.referrals import (
    AUTH_STATUS_VALUES,
    EVENT_TYPE_VALUES,
    STATUS_TRANSITIONS,
    STATUS_VALUES,
    TERMINAL_STATUSES,
    URGENCY_VALUES,
    InvalidTransition,
    Referral,
    ReferralEvent,
    require_transition,
    transition_allowed,
)
from docstats.scope import Scope, ScopeRequired
from docstats.storage import Storage, _to_sqlite_utc_iso


# --- Fixtures ---


@pytest.fixture
def user_a(storage: Storage) -> int:
    return storage.create_user("a@ref.com", "hashed")


@pytest.fixture
def user_b(storage: Storage) -> int:
    return storage.create_user("b@ref.com", "hashed")


@pytest.fixture
def scope_a(user_a: int) -> Scope:
    return Scope(user_id=user_a)


@pytest.fixture
def scope_b(user_b: int) -> Scope:
    return Scope(user_id=user_b)


@pytest.fixture
def patient_a(storage: Storage, scope_a: Scope) -> int:
    return storage.create_patient(scope_a, first_name="Jane", last_name="Doe").id


@pytest.fixture
def patient_b(storage: Storage, scope_b: Scope) -> int:
    return storage.create_patient(scope_b, first_name="John", last_name="Smith").id


# --- State machine ---


def test_all_status_values_have_transition_entries() -> None:
    """Every status must appear as a key in STATUS_TRANSITIONS — otherwise
    transition_allowed returns False for valid statuses."""
    for s in STATUS_VALUES:
        assert s in STATUS_TRANSITIONS


def test_terminal_statuses_have_no_outgoing_edges() -> None:
    for s in TERMINAL_STATUSES:
        assert STATUS_TRANSITIONS[s] == frozenset()


def test_transition_allowed_happy_path() -> None:
    assert transition_allowed("draft", "ready") is True
    assert transition_allowed("ready", "sent") is True
    assert transition_allowed("sent", "scheduled") is True
    assert transition_allowed("scheduled", "completed") is True


def test_transition_allowed_rejects_illegal_edge() -> None:
    assert transition_allowed("draft", "completed") is False
    assert transition_allowed("completed", "draft") is False  # terminal
    assert transition_allowed("cancelled", "sent") is False  # terminal


def test_transition_allowed_rejects_unknown_from_status() -> None:
    """Defensive: stale DB data shouldn't crash the state machine."""
    assert transition_allowed("mystery_status", "draft") is False


def test_require_transition_raises_on_invalid() -> None:
    with pytest.raises(InvalidTransition, match="draft"):
        require_transition("draft", "completed")


def test_require_transition_silent_on_valid() -> None:
    require_transition("draft", "ready")  # should not raise


def test_transition_graph_is_connected_from_draft() -> None:
    """Every non-terminal status must be reachable from draft via BFS —
    otherwise a referral could get orphaned in an unreachable state."""
    visited = {"draft"}
    frontier = ["draft"]
    while frontier:
        node = frontier.pop()
        for nxt in STATUS_TRANSITIONS[node]:
            if nxt not in visited:
                visited.add(nxt)
                frontier.append(nxt)
    unreachable = set(STATUS_VALUES) - visited
    assert unreachable == set(), f"Unreachable statuses: {unreachable}"


# --- create_referral ---


def test_create_referral_solo_happy_path(
    storage: Storage, scope_a: Scope, patient_a: int, user_a: int
) -> None:
    r = storage.create_referral(
        scope_a,
        patient_id=patient_a,
        receiving_provider_npi="1234567890",
        specialty_code="207R00000X",
        reason="Chest pain for 3 weeks",
        urgency="priority",
        created_by_user_id=user_a,
    )
    assert isinstance(r, Referral)
    assert r.patient_id == patient_a
    assert r.scope_user_id == scope_a.user_id
    assert r.scope_organization_id is None
    assert r.status == "draft"
    assert r.urgency == "priority"
    assert r.authorization_status == "na_unknown"
    assert r.external_source == "manual"
    assert r.deleted_at is None
    assert r.is_terminal is False


def test_create_referral_seeds_created_event(
    storage: Storage, scope_a: Scope, patient_a: int, user_a: int
) -> None:
    r = storage.create_referral(scope_a, patient_id=patient_a, created_by_user_id=user_a)
    events = storage.list_referral_events(scope_a, r.id)
    assert len(events) == 1
    assert events[0].event_type == "created"
    assert events[0].to_value == "draft"
    assert events[0].actor_user_id == user_a


def test_create_referral_rejects_anonymous_scope(storage: Storage, patient_a: int) -> None:
    with pytest.raises(ScopeRequired):
        storage.create_referral(Scope(), patient_id=patient_a)


def test_create_referral_rejects_cross_scope_patient(
    storage: Storage, scope_a: Scope, patient_b: int
) -> None:
    """Critical: scope A cannot create a referral pointing at patient B.
    Prevents cross-tenant FK forgery."""
    with pytest.raises(ValueError, match="not found in scope"):
        storage.create_referral(scope_a, patient_id=patient_b)


def test_create_referral_rejects_soft_deleted_patient(
    storage: Storage, scope_a: Scope, patient_a: int
) -> None:
    storage.soft_delete_patient(scope_a, patient_a)
    with pytest.raises(ValueError, match="not found in scope"):
        storage.create_referral(scope_a, patient_id=patient_a)


def test_create_referral_rejects_unknown_enums(
    storage: Storage, scope_a: Scope, patient_a: int
) -> None:
    with pytest.raises(ValueError, match="urgency"):
        storage.create_referral(scope_a, patient_id=patient_a, urgency="yesterday")
    with pytest.raises(ValueError, match="status"):
        storage.create_referral(scope_a, patient_id=patient_a, status="bogus")
    with pytest.raises(ValueError, match="authorization_status"):
        storage.create_referral(scope_a, patient_id=patient_a, authorization_status="maybe")


def test_create_referral_check_constraint_on_scope_keys(storage: Storage, patient_a: int) -> None:
    """Direct SQL with both scope cols NULL must be rejected by the DB."""
    with pytest.raises(sqlite3.IntegrityError):
        storage._conn.execute(
            "INSERT INTO referrals (scope_user_id, scope_organization_id, patient_id) "
            "VALUES (NULL, NULL, ?)",
            (patient_a,),
        )
        storage._conn.commit()
    storage._conn.rollback()


# --- Cross-tenant read isolation ---


def test_get_referral_cross_tenant_returns_none(
    storage: Storage,
    scope_a: Scope,
    scope_b: Scope,
    patient_a: int,
) -> None:
    r = storage.create_referral(scope_a, patient_id=patient_a)
    assert storage.get_referral(scope_b, r.id) is None
    assert storage.get_referral(scope_a, r.id) is not None


def test_list_referrals_scope_filtered(
    storage: Storage,
    scope_a: Scope,
    scope_b: Scope,
    patient_a: int,
    patient_b: int,
) -> None:
    storage.create_referral(scope_a, patient_id=patient_a)
    storage.create_referral(scope_a, patient_id=patient_a)
    storage.create_referral(scope_b, patient_id=patient_b)
    assert len(storage.list_referrals(scope_a)) == 2
    assert len(storage.list_referrals(scope_b)) == 1


def test_list_referrals_anonymous_raises(storage: Storage) -> None:
    with pytest.raises(ScopeRequired):
        storage.list_referrals(Scope())


def test_list_referrals_filter_by_patient(storage: Storage, scope_a: Scope, patient_a: int) -> None:
    other_patient = storage.create_patient(scope_a, first_name="Other", last_name="Pt").id
    storage.create_referral(scope_a, patient_id=patient_a)
    storage.create_referral(scope_a, patient_id=patient_a)
    storage.create_referral(scope_a, patient_id=other_patient)
    only_a = storage.list_referrals(scope_a, patient_id=patient_a)
    assert len(only_a) == 2
    assert all(r.patient_id == patient_a for r in only_a)


def test_list_referrals_filter_by_status_and_assignee(
    storage: Storage, scope_a: Scope, patient_a: int, user_a: int
) -> None:
    r = storage.create_referral(scope_a, patient_id=patient_a, assigned_to_user_id=user_a)
    storage.create_referral(scope_a, patient_id=patient_a)  # unassigned

    storage.set_referral_status(scope_a, r.id, "ready")

    ready = storage.list_referrals(scope_a, status="ready")
    assert len(ready) == 1
    assert ready[0].id == r.id

    mine = storage.list_referrals(scope_a, assigned_to_user_id=user_a)
    assert len(mine) == 1
    assert mine[0].id == r.id


def test_list_referrals_orders_by_updated_desc(
    storage: Storage, scope_a: Scope, patient_a: int
) -> None:
    import time

    r1 = storage.create_referral(scope_a, patient_id=patient_a)
    r2 = storage.create_referral(scope_a, patient_id=patient_a)
    # SQLite datetime('now') is second-granularity; sleep past the boundary
    # so r1's touch lands strictly later than r2's creation.
    time.sleep(1.05)
    storage.update_referral(scope_a, r1.id, reason="Updated")
    ids = [r.id for r in storage.list_referrals(scope_a)]
    assert ids[0] == r1.id
    assert ids[1] == r2.id


def test_list_referrals_excludes_deleted_by_default(
    storage: Storage, scope_a: Scope, patient_a: int
) -> None:
    r = storage.create_referral(scope_a, patient_id=patient_a)
    storage.soft_delete_referral(scope_a, r.id)
    assert storage.list_referrals(scope_a) == []
    assert len(storage.list_referrals(scope_a, include_deleted=True)) == 1


def test_count_referrals_filters_by_updated_before(
    storage: Storage, scope_a: Scope, patient_a: int
) -> None:
    old = storage.create_referral(scope_a, patient_id=patient_a, status="awaiting_records")
    fresh = storage.create_referral(scope_a, patient_id=patient_a, status="awaiting_auth")
    other_status = storage.create_referral(scope_a, patient_id=patient_a, status="sent")
    old_cutoff = datetime.now(timezone.utc) - timedelta(days=4)
    storage._conn.execute(
        "UPDATE referrals SET updated_at = ? WHERE id IN (?, ?)",
        (_to_sqlite_utc_iso(old_cutoff), old.id, other_status.id),
    )
    storage._conn.commit()

    count = storage.count_referrals(
        scope_a,
        statuses=("awaiting_records", "awaiting_auth"),
        updated_before=datetime.now(timezone.utc) - timedelta(days=3),
    )

    assert count == 1
    assert storage.get_referral(scope_a, fresh.id) is not None


# --- update_referral ---


def test_update_referral_fields(storage: Storage, scope_a: Scope, patient_a: int) -> None:
    r = storage.create_referral(scope_a, patient_id=patient_a)
    updated = storage.update_referral(
        scope_a,
        r.id,
        reason="Specialist follow-up needed",
        urgency="urgent",
        authorization_status="required_pending",
    )
    assert updated is not None
    assert updated.reason == "Specialist follow-up needed"
    assert updated.urgency == "urgent"
    assert updated.authorization_status == "required_pending"


def test_update_referral_cross_tenant_returns_none(
    storage: Storage,
    scope_a: Scope,
    scope_b: Scope,
    patient_a: int,
) -> None:
    r = storage.create_referral(scope_a, patient_id=patient_a)
    assert storage.update_referral(scope_b, r.id, reason="Hijacked") is None
    # A's row is untouched.
    re = storage.get_referral(scope_a, r.id)
    assert re is not None
    assert re.reason is None


def test_update_referral_rejects_unknown_enum(
    storage: Storage, scope_a: Scope, patient_a: int
) -> None:
    r = storage.create_referral(scope_a, patient_id=patient_a)
    with pytest.raises(ValueError, match="urgency"):
        storage.update_referral(scope_a, r.id, urgency="someday")


# --- set_referral_status ---


def test_set_referral_status_changes_value(
    storage: Storage, scope_a: Scope, patient_a: int
) -> None:
    r = storage.create_referral(scope_a, patient_id=patient_a)
    updated = storage.set_referral_status(scope_a, r.id, "ready")
    assert updated is not None
    assert updated.status == "ready"


def test_set_referral_status_rejects_unknown_status(
    storage: Storage, scope_a: Scope, patient_a: int
) -> None:
    r = storage.create_referral(scope_a, patient_id=patient_a)
    with pytest.raises(ValueError, match="status"):
        storage.set_referral_status(scope_a, r.id, "in_progress")


def test_set_referral_status_cross_tenant_returns_none(
    storage: Storage,
    scope_a: Scope,
    scope_b: Scope,
    patient_a: int,
) -> None:
    r = storage.create_referral(scope_a, patient_id=patient_a)
    assert storage.set_referral_status(scope_b, r.id, "ready") is None


# --- soft_delete_referral ---


def test_soft_delete_referral(storage: Storage, scope_a: Scope, patient_a: int) -> None:
    r = storage.create_referral(scope_a, patient_id=patient_a)
    assert storage.soft_delete_referral(scope_a, r.id) is True
    assert storage.get_referral(scope_a, r.id) is None
    assert storage.soft_delete_referral(scope_a, r.id) is False  # no-op


def test_soft_delete_referral_cross_tenant_returns_false(
    storage: Storage,
    scope_a: Scope,
    scope_b: Scope,
    patient_a: int,
) -> None:
    r = storage.create_referral(scope_a, patient_id=patient_a)
    assert storage.soft_delete_referral(scope_b, r.id) is False
    assert storage.get_referral(scope_a, r.id) is not None


def test_patient_with_referral_cannot_be_hard_deleted(
    storage: Storage, scope_a: Scope, patient_a: int
) -> None:
    """ON DELETE RESTRICT on the patient FK protects referrals from dangling.
    Soft-delete remains fine; hard-delete is prevented."""
    storage.create_referral(scope_a, patient_id=patient_a)
    with pytest.raises(sqlite3.IntegrityError):
        storage._conn.execute("DELETE FROM patients WHERE id = ?", (patient_a,))
        storage._conn.commit()
    storage._conn.rollback()


# --- Referral events ---


def test_record_and_list_referral_event(
    storage: Storage, scope_a: Scope, patient_a: int, user_a: int
) -> None:
    r = storage.create_referral(scope_a, patient_id=patient_a, created_by_user_id=user_a)
    event = storage.record_referral_event(
        scope_a,
        r.id,
        event_type="status_changed",
        from_value="draft",
        to_value="ready",
        actor_user_id=user_a,
        note="Ready for sign-off",
    )
    assert isinstance(event, ReferralEvent)
    assert event.event_type == "status_changed"
    assert event.from_value == "draft"
    assert event.to_value == "ready"

    events = storage.list_referral_events(scope_a, r.id)
    # Newest first: status_changed, then created.
    assert len(events) == 2
    assert events[0].event_type == "status_changed"
    assert events[1].event_type == "created"


def test_record_event_rejects_unknown_type(
    storage: Storage, scope_a: Scope, patient_a: int
) -> None:
    r = storage.create_referral(scope_a, patient_id=patient_a)
    with pytest.raises(ValueError, match="event_type"):
        storage.record_referral_event(scope_a, r.id, event_type="invented")


def test_record_event_cross_tenant_returns_none(
    storage: Storage,
    scope_a: Scope,
    scope_b: Scope,
    patient_a: int,
) -> None:
    """Writing an event for a referral in another scope silently returns None
    — avoids leaking the existence of out-of-scope referrals."""
    r = storage.create_referral(scope_a, patient_id=patient_a)
    assert storage.record_referral_event(scope_b, r.id, event_type="note_added") is None
    # No stray event was written.
    assert len(storage.list_referral_events(scope_a, r.id)) == 1  # just the 'created'


def test_list_events_cross_tenant_returns_empty(
    storage: Storage,
    scope_a: Scope,
    scope_b: Scope,
    patient_a: int,
) -> None:
    r = storage.create_referral(scope_a, patient_id=patient_a)
    storage.record_referral_event(scope_a, r.id, event_type="note_added", note="x")
    assert storage.list_referral_events(scope_b, r.id) == []


def test_soft_deleted_referral_events_not_listable(
    storage: Storage, scope_a: Scope, patient_a: int
) -> None:
    """Once the referral is soft-deleted, events for it are hidden (list
    returns empty) because get_referral filters out deleted rows."""
    r = storage.create_referral(scope_a, patient_id=patient_a)
    storage.record_referral_event(scope_a, r.id, event_type="note_added", note="x")
    storage.soft_delete_referral(scope_a, r.id)
    assert storage.list_referral_events(scope_a, r.id) == []


def test_hard_delete_cascades_events(storage: Storage, scope_a: Scope, patient_a: int) -> None:
    """Hard-deleting a referral (admin purge) cascades to events."""
    r = storage.create_referral(scope_a, patient_id=patient_a)
    storage.record_referral_event(scope_a, r.id, event_type="note_added", note="x")
    storage._conn.execute("DELETE FROM referrals WHERE id = ?", (r.id,))
    storage._conn.commit()
    row = storage._conn.execute(
        "SELECT count(*) AS n FROM referral_events WHERE referral_id = ?",
        (r.id,),
    ).fetchone()
    assert row["n"] == 0


# --- Enum exhaustiveness (catches mismatched SQL CHECK + Python constants) ---


def test_all_urgency_values_pass_create(storage: Storage, scope_a: Scope, patient_a: int) -> None:
    """Every URGENCY_VALUES entry must be writable — catches drift between
    the Python constants and the SQL CHECK constraint."""
    for u in URGENCY_VALUES:
        r = storage.create_referral(scope_a, patient_id=patient_a, urgency=u)
        assert r.urgency == u


def test_all_auth_status_values_pass_create(
    storage: Storage, scope_a: Scope, patient_a: int
) -> None:
    for s in AUTH_STATUS_VALUES:
        r = storage.create_referral(scope_a, patient_id=patient_a, authorization_status=s)
        assert r.authorization_status == s


def test_all_status_values_pass_set_status(
    storage: Storage, scope_a: Scope, patient_a: int
) -> None:
    r = storage.create_referral(scope_a, patient_id=patient_a)
    for s in STATUS_VALUES:
        updated = storage.set_referral_status(scope_a, r.id, s)
        assert updated is not None
        assert updated.status == s


def test_all_event_type_values_pass_record(
    storage: Storage, scope_a: Scope, patient_a: int
) -> None:
    r = storage.create_referral(scope_a, patient_id=patient_a)
    for et in EVENT_TYPE_VALUES:
        e = storage.record_referral_event(scope_a, r.id, event_type=et)
        assert e is not None
        assert e.event_type == et
