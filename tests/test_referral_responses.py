"""Tests for referral_responses — closed-loop capture (Phase 1.D)."""

from __future__ import annotations

import pytest

from docstats.domain.referrals import RECEIVED_VIA_VALUES, ReferralResponse
from docstats.scope import Scope
from docstats.storage import Storage


# --- Fixtures ---


@pytest.fixture
def user_a(storage: Storage) -> int:
    return storage.create_user("a@resp.com", "hashed")


@pytest.fixture
def user_b(storage: Storage) -> int:
    return storage.create_user("b@resp.com", "hashed")


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
def referral_a(storage: Storage, scope_a: Scope, patient_a: int) -> int:
    return storage.create_referral(scope_a, patient_id=patient_a).id


# --- Happy path ---


def test_record_response_minimal(
    storage: Storage, scope_a: Scope, referral_a: int, user_a: int
) -> None:
    r = storage.record_referral_response(
        scope_a,
        referral_a,
        appointment_date="2026-05-01",
        received_via="fax",
        recorded_by_user_id=user_a,
    )
    assert isinstance(r, ReferralResponse)
    assert r.referral_id == referral_a
    assert r.appointment_date == "2026-05-01"
    assert r.consult_completed is False  # default
    assert r.received_via == "fax"
    assert r.recorded_by_user_id == user_a


def test_record_response_full_closed_loop(
    storage: Storage, scope_a: Scope, referral_a: int, user_a: int
) -> None:
    """Completed consult with recommendations — the terminal closed-loop state."""
    r = storage.record_referral_response(
        scope_a,
        referral_a,
        appointment_date="2026-05-01",
        consult_completed=True,
        recommendations_text="Follow up in 3 months. Start statin.",
        received_via="portal",
        recorded_by_user_id=user_a,
    )
    assert r is not None
    assert r.consult_completed is True
    assert r.recommendations_text == "Follow up in 3 months. Start statin."


def test_list_responses_newest_first(storage: Storage, scope_a: Scope, referral_a: int) -> None:
    """Two responses on one referral — scheduled then completed. list orders
    newest first so the UI shows the latest state."""
    import time

    scheduled = storage.record_referral_response(
        scope_a,
        referral_a,
        appointment_date="2026-05-01",
        received_via="phone",
    )
    assert scheduled is not None
    time.sleep(1.05)  # SQLite datetime('now') is second-granular
    completed = storage.record_referral_response(
        scope_a,
        referral_a,
        appointment_date="2026-05-01",
        consult_completed=True,
        recommendations_text="Done",
        received_via="fax",
    )
    assert completed is not None

    listed = storage.list_referral_responses(scope_a, referral_a)
    assert len(listed) == 2
    assert listed[0].id == completed.id
    assert listed[1].id == scheduled.id


# --- Scope isolation ---


def test_record_response_cross_tenant_returns_none(
    storage: Storage, scope_b: Scope, referral_a: int
) -> None:
    """Writing into another tenant's referral silently returns None."""
    assert (
        storage.record_referral_response(scope_b, referral_a, appointment_date="2026-05-01") is None
    )
    # No row was written.
    assert (
        storage._conn.execute(
            "SELECT count(*) AS n FROM referral_responses WHERE referral_id = ?",
            (referral_a,),
        ).fetchone()["n"]
        == 0
    )


def test_list_responses_cross_tenant_returns_empty(
    storage: Storage, scope_a: Scope, scope_b: Scope, referral_a: int
) -> None:
    storage.record_referral_response(scope_a, referral_a, received_via="fax")
    assert storage.list_referral_responses(scope_b, referral_a) == []


def test_update_response_cross_tenant_returns_none(
    storage: Storage, scope_a: Scope, scope_b: Scope, referral_a: int
) -> None:
    r = storage.record_referral_response(scope_a, referral_a, received_via="fax")
    assert r is not None
    assert (
        storage.update_referral_response(scope_b, referral_a, r.id, recommendations_text="Hijack")
        is None
    )
    # A's row untouched.
    fresh = storage.list_referral_responses(scope_a, referral_a)[0]
    assert fresh.recommendations_text is None


def test_delete_response_cross_tenant_returns_false(
    storage: Storage, scope_a: Scope, scope_b: Scope, referral_a: int
) -> None:
    r = storage.record_referral_response(scope_a, referral_a, received_via="fax")
    assert r is not None
    assert storage.delete_referral_response(scope_b, referral_a, r.id) is False
    assert len(storage.list_referral_responses(scope_a, referral_a)) == 1


# --- Update semantics ---


def test_update_response_partial_fields(storage: Storage, scope_a: Scope, referral_a: int) -> None:
    r = storage.record_referral_response(
        scope_a, referral_a, appointment_date="2026-05-01", received_via="phone"
    )
    assert r is not None
    updated = storage.update_referral_response(
        scope_a,
        referral_a,
        r.id,
        consult_completed=True,
        recommendations_text="All good",
    )
    assert updated is not None
    assert updated.consult_completed is True
    assert updated.recommendations_text == "All good"
    assert updated.appointment_date == "2026-05-01"  # untouched
    assert updated.received_via == "phone"  # untouched
    assert updated.updated_at >= r.updated_at


def test_update_response_rejects_unknown_received_via(
    storage: Storage, scope_a: Scope, referral_a: int
) -> None:
    r = storage.record_referral_response(scope_a, referral_a, received_via="fax")
    assert r is not None
    with pytest.raises(ValueError, match="received_via"):
        storage.update_referral_response(scope_a, referral_a, r.id, received_via="carrier_pigeon")


def test_update_response_no_fields_returns_current(
    storage: Storage, scope_a: Scope, referral_a: int
) -> None:
    r = storage.record_referral_response(scope_a, referral_a, received_via="fax")
    assert r is not None
    current = storage.update_referral_response(scope_a, referral_a, r.id)
    assert current is not None
    assert current.id == r.id


# --- Delete ---


def test_delete_response(storage: Storage, scope_a: Scope, referral_a: int) -> None:
    r = storage.record_referral_response(scope_a, referral_a, received_via="fax")
    assert r is not None
    assert storage.delete_referral_response(scope_a, referral_a, r.id) is True
    assert storage.list_referral_responses(scope_a, referral_a) == []
    # Double-delete is a no-op.
    assert storage.delete_referral_response(scope_a, referral_a, r.id) is False


# --- Enum validation ---


def test_record_response_rejects_unknown_received_via(
    storage: Storage, scope_a: Scope, referral_a: int
) -> None:
    with pytest.raises(ValueError, match="received_via"):
        storage.record_referral_response(scope_a, referral_a, received_via="time_travel")


def test_all_received_via_values_pass_record(
    storage: Storage, scope_a: Scope, referral_a: int
) -> None:
    """Every Python constant must be writable through the SQL CHECK — catches
    drift between the two."""
    for v in RECEIVED_VIA_VALUES:
        r = storage.record_referral_response(scope_a, referral_a, received_via=v)
        assert r is not None
        assert r.received_via == v


# --- Cascade behavior ---


def test_soft_delete_referral_hides_responses(
    storage: Storage, scope_a: Scope, referral_a: int
) -> None:
    storage.record_referral_response(
        scope_a, referral_a, appointment_date="2026-05-01", received_via="fax"
    )
    storage.soft_delete_referral(scope_a, referral_a)
    # Hidden via the scope gate, but evidence row still in DB.
    assert storage.list_referral_responses(scope_a, referral_a) == []
    assert (
        storage._conn.execute(
            "SELECT count(*) AS n FROM referral_responses WHERE referral_id = ?",
            (referral_a,),
        ).fetchone()["n"]
        == 1
    )


def test_hard_delete_referral_cascades_responses(
    storage: Storage, scope_a: Scope, referral_a: int
) -> None:
    storage.record_referral_response(scope_a, referral_a, received_via="fax")
    storage._conn.execute("DELETE FROM referrals WHERE id = ?", (referral_a,))
    storage._conn.commit()
    row = storage._conn.execute(
        "SELECT count(*) AS n FROM referral_responses WHERE referral_id = ?",
        (referral_a,),
    ).fetchone()
    assert row["n"] == 0
