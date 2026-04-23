"""Phase 9.A — Delivery storage + dispatcher tests.

Covers:

- Storage CRUD + scope isolation (cross-tenant can't see / cancel)
- Sweeper query (`list_deliveries_ready_for_dispatch`) picks up queued
  rows immediately + stuck sending rows after threshold
- ``record_delivery_attempt_*`` pairing
- Channel registry reports no channels enabled in 9.A (all raise
  ``ChannelDisabledError``)
- ``_process_one`` dispatcher helper handles success / retryable /
  fatal / channel-disabled paths
- ``referral_events.event_type`` CHECK constraint accepts the three new
  delivery event types (regression on the migration)
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from docstats.delivery.base import (
    ChannelDisabledError,
    DeliveryError,
    DeliveryReceipt,
)
from docstats.delivery.dispatcher import _process_one
from docstats.delivery.registry import enabled_channels, get_channel
from docstats.domain.deliveries import (
    DELIVERY_STATUS_VALUES,
    PICKUP_DELIVERY_STATUSES,
    TERMINAL_DELIVERY_STATUSES,
)
from docstats.scope import Scope
from docstats.storage import Storage


# ---------- Fixtures ----------


def _seed(storage: Storage, user_id: int):
    scope = Scope(user_id=user_id)
    patient = storage.create_patient(
        scope,
        first_name="Jane",
        last_name="Doe",
        date_of_birth="1980-05-15",
        created_by_user_id=user_id,
    )
    referral = storage.create_referral(
        scope,
        patient_id=patient.id,
        reason="Chest pain eval",
        urgency="urgent",
        specialty_desc="Cardiology",
        receiving_organization_name="Heart Clinic",
        created_by_user_id=user_id,
    )
    return scope, patient, referral


@pytest.fixture
def storage(tmp_path: Path):
    s = Storage(db_path=tmp_path / "test.db")
    yield s
    s.close()


# ---------- Registry (Phase 9.A ships with zero enabled channels) ----------


def test_enabled_channels_is_empty_in_9a():
    """Every channel raises ChannelDisabledError until a vendor lands."""
    assert enabled_channels() == []


@pytest.mark.parametrize("channel", ["email", "fax", "direct"])
def test_get_channel_raises_channel_disabled(channel):
    with pytest.raises(ChannelDisabledError):
        get_channel(channel)


def test_get_channel_unknown_name():
    with pytest.raises(ChannelDisabledError) as exc:
        get_channel("bogus")
    assert "unknown channel" in str(exc.value).lower()


# ---------- Storage CRUD ----------


def test_create_delivery_denormalizes_scope_from_referral(storage: Storage):
    user_id = storage.create_user("a@example.com", "hashed")
    scope, _, referral = _seed(storage, user_id)
    d = storage.create_delivery(
        scope,
        referral_id=referral.id,
        channel="fax",
        recipient="+14155551234",
    )
    assert d.id > 0
    assert d.status == "queued"
    assert d.scope_user_id == user_id
    assert d.scope_organization_id is None
    assert d.retry_count == 0


def test_create_delivery_rejects_cross_scope_referral(storage: Storage):
    a = storage.create_user("a@example.com", "hashed")
    b = storage.create_user("b@example.com", "hashed")
    scope_a, _, referral_a = _seed(storage, a)
    scope_b = Scope(user_id=b)
    with pytest.raises(ValueError):
        storage.create_delivery(
            scope_b,
            referral_id=referral_a.id,
            channel="fax",
            recipient="+14155551234",
        )


def test_get_delivery_scope_isolation(storage: Storage):
    a = storage.create_user("a@example.com", "hashed")
    b = storage.create_user("b@example.com", "hashed")
    scope_a, _, referral_a = _seed(storage, a)
    scope_b = Scope(user_id=b)
    d = storage.create_delivery(
        scope_a, referral_id=referral_a.id, channel="fax", recipient="+14155551234"
    )
    # Owner can see it.
    assert storage.get_delivery(scope_a, d.id) is not None
    # Other scope can't.
    assert storage.get_delivery(scope_b, d.id) is None
    # Dispatcher (scope=None) always can.
    assert storage.get_delivery(None, d.id) is not None


def test_cancel_delivery_flips_status_and_records_actor(storage: Storage):
    user_id = storage.create_user("a@example.com", "hashed")
    scope, _, referral = _seed(storage, user_id)
    d = storage.create_delivery(
        scope, referral_id=referral.id, channel="fax", recipient="+14155551234"
    )
    ok = storage.cancel_delivery(scope, d.id, cancelled_by_user_id=user_id)
    assert ok is True
    refreshed = storage.get_delivery(scope, d.id)
    assert refreshed is not None
    assert refreshed.status == "cancelled"
    assert refreshed.cancelled_by_user_id == user_id
    assert refreshed.cancelled_at is not None


def test_cancel_delivery_idempotent_noop_on_terminal(storage: Storage):
    user_id = storage.create_user("a@example.com", "hashed")
    scope, _, referral = _seed(storage, user_id)
    d = storage.create_delivery(
        scope, referral_id=referral.id, channel="fax", recipient="+14155551234"
    )
    storage.mark_delivery_sent(d.id, vendor_name="Test", vendor_message_id="m1")
    # Flip to delivered (terminal).
    storage.mark_delivery_sent(d.id, vendor_name="Test", vendor_message_id="m1", status="delivered")
    assert storage.cancel_delivery(scope, d.id, cancelled_by_user_id=user_id) is False


def test_cancel_delivery_cross_scope_blocked(storage: Storage):
    a = storage.create_user("a@example.com", "hashed")
    b = storage.create_user("b@example.com", "hashed")
    scope_a, _, referral_a = _seed(storage, a)
    scope_b = Scope(user_id=b)
    d = storage.create_delivery(
        scope_a, referral_id=referral_a.id, channel="fax", recipient="+14155551234"
    )
    assert storage.cancel_delivery(scope_b, d.id, cancelled_by_user_id=b) is False


def test_list_deliveries_for_referral_newest_first(storage: Storage):
    user_id = storage.create_user("a@example.com", "hashed")
    scope, _, referral = _seed(storage, user_id)
    d1 = storage.create_delivery(
        scope, referral_id=referral.id, channel="fax", recipient="+14155551234"
    )
    d2 = storage.create_delivery(
        scope, referral_id=referral.id, channel="email", recipient="foo@bar.com"
    )
    rows = storage.list_deliveries_for_referral(scope, referral.id)
    assert [r.id for r in rows][0] == d2.id  # newest first
    assert d1.id in [r.id for r in rows]


# ---------- Sweeper query ----------


def test_ready_for_dispatch_picks_queued_immediately(storage: Storage):
    user_id = storage.create_user("a@example.com", "hashed")
    scope, _, referral = _seed(storage, user_id)
    d = storage.create_delivery(
        scope, referral_id=referral.id, channel="fax", recipient="+14155551234"
    )
    ready = storage.list_deliveries_ready_for_dispatch()
    assert d.id in [r.id for r in ready]


def test_ready_for_dispatch_skips_sent_and_failed(storage: Storage):
    user_id = storage.create_user("a@example.com", "hashed")
    scope, _, referral = _seed(storage, user_id)
    d_sent = storage.create_delivery(
        scope, referral_id=referral.id, channel="fax", recipient="+14155551234"
    )
    storage.mark_delivery_sent(d_sent.id, vendor_name="v", vendor_message_id="m")
    d_failed = storage.create_delivery(
        scope, referral_id=referral.id, channel="fax", recipient="+14155551234"
    )
    storage.mark_delivery_failed(d_failed.id, error_code="x", error_message="y")
    ready = [r.id for r in storage.list_deliveries_ready_for_dispatch()]
    assert d_sent.id not in ready
    assert d_failed.id not in ready


def test_ready_for_dispatch_skips_fresh_sending_but_picks_stale(storage: Storage):
    user_id = storage.create_user("a@example.com", "hashed")
    scope, _, referral = _seed(storage, user_id)
    d_fresh = storage.create_delivery(
        scope, referral_id=referral.id, channel="fax", recipient="+14155551234"
    )
    storage.mark_delivery_sending(d_fresh.id)
    # Fresh sending row — stuck threshold default 120s; not picked up.
    ready = [r.id for r in storage.list_deliveries_ready_for_dispatch()]
    assert d_fresh.id not in ready
    # Manually backdate updated_at to simulate a stuck row (SQLite
    # datetime is second-precision so a sleep-based test would be
    # flaky).
    storage._conn.execute(
        "UPDATE deliveries SET updated_at = datetime('now', '-5 minutes') WHERE id = ?",
        (d_fresh.id,),
    )
    storage._conn.commit()
    ready = [r.id for r in storage.list_deliveries_ready_for_dispatch(stuck_sending_seconds=60)]
    assert d_fresh.id in ready


# ---------- Attempt rows ----------


def test_attempt_row_round_trip(storage: Storage):
    user_id = storage.create_user("a@example.com", "hashed")
    scope, _, referral = _seed(storage, user_id)
    d = storage.create_delivery(
        scope, referral_id=referral.id, channel="fax", recipient="+14155551234"
    )
    attempt_id = storage.record_delivery_attempt_start(delivery_id=d.id, attempt_number=1)
    storage.record_delivery_attempt_complete(
        attempt_id=attempt_id,
        result="success",
        vendor_response_excerpt="OK",
    )
    attempts = storage.list_delivery_attempts(scope, d.id)
    assert len(attempts) == 1
    assert attempts[0].result == "success"
    assert attempts[0].completed_at is not None


def test_requeue_bumps_retry_count(storage: Storage):
    user_id = storage.create_user("a@example.com", "hashed")
    scope, _, referral = _seed(storage, user_id)
    d = storage.create_delivery(
        scope, referral_id=referral.id, channel="fax", recipient="+14155551234"
    )
    storage.mark_delivery_sending(d.id)
    storage.requeue_delivery_for_retry(d.id, error_code="vendor_5xx", error_message="boom")
    refreshed = storage.get_delivery(scope, d.id)
    assert refreshed is not None
    assert refreshed.status == "queued"
    assert refreshed.retry_count == 1
    assert refreshed.last_error_code == "vendor_5xx"


# ---------- Event-type migration regression ----------


def test_referral_events_accept_delivery_event_types(storage: Storage):
    """Migration 015 must extend the CHECK constraint to allow the three
    new event types. If this test fails the SQLite rebuild skipped a
    deployment environment."""
    user_id = storage.create_user("a@example.com", "hashed")
    scope, _, referral = _seed(storage, user_id)
    for evt in ("dispatched", "delivered", "delivery_failed"):
        ev = storage.record_referral_event(scope, referral.id, event_type=evt, note=f"test-{evt}")
        assert ev is not None, f"CHECK constraint rejected {evt!r}"


# ---------- Dispatcher.process_one ----------


class _SuccessChannel:
    name = "fax"
    vendor_name = "FakeVendor"

    async def send(self, delivery, packet_bytes):
        return DeliveryReceipt(
            vendor_name="FakeVendor",
            vendor_message_id="msg-123",
            status="sent",
            vendor_response_excerpt="accepted",
        )

    async def poll_status(self, delivery):
        return None


class _RetryableChannel:
    name = "fax"
    vendor_name = "FakeVendor"

    async def send(self, delivery, packet_bytes):
        raise DeliveryError(error_code="vendor_5xx", message="boom", retryable=True)

    async def poll_status(self, delivery):
        return None


class _FatalChannel:
    name = "fax"
    vendor_name = "FakeVendor"

    async def send(self, delivery, packet_bytes):
        raise DeliveryError(error_code="invalid_recipient", message="malformed", retryable=False)

    async def poll_status(self, delivery):
        return None


async def _noop_render(delivery):
    return b"%PDF-fake"


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


def test_process_one_success_path(storage: Storage):
    user_id = storage.create_user("a@example.com", "hashed")
    scope, _, referral = _seed(storage, user_id)
    d = storage.create_delivery(
        scope, referral_id=referral.id, channel="fax", recipient="+14155551234"
    )
    asyncio.run(
        _process_one(
            storage,
            d,
            channel_factory=lambda name: _SuccessChannel(),
            render_packet=_noop_render,
        )
    )
    refreshed = storage.get_delivery(scope, d.id)
    assert refreshed is not None
    assert refreshed.status == "sent"
    assert refreshed.vendor_message_id == "msg-123"
    assert refreshed.sent_at is not None
    attempts = storage.list_delivery_attempts(scope, d.id)
    assert len(attempts) == 1
    assert attempts[0].result == "success"


def test_process_one_retryable_path(storage: Storage):
    user_id = storage.create_user("a@example.com", "hashed")
    scope, _, referral = _seed(storage, user_id)
    d = storage.create_delivery(
        scope, referral_id=referral.id, channel="fax", recipient="+14155551234"
    )
    asyncio.run(
        _process_one(
            storage,
            d,
            channel_factory=lambda name: _RetryableChannel(),
            render_packet=_noop_render,
        )
    )
    refreshed = storage.get_delivery(scope, d.id)
    assert refreshed is not None
    assert refreshed.status == "queued"  # back on the queue
    assert refreshed.retry_count == 1
    assert refreshed.last_error_code == "vendor_5xx"


def test_process_one_fatal_path(storage: Storage):
    user_id = storage.create_user("a@example.com", "hashed")
    scope, _, referral = _seed(storage, user_id)
    d = storage.create_delivery(
        scope, referral_id=referral.id, channel="fax", recipient="+14155551234"
    )
    asyncio.run(
        _process_one(
            storage,
            d,
            channel_factory=lambda name: _FatalChannel(),
            render_packet=_noop_render,
        )
    )
    refreshed = storage.get_delivery(scope, d.id)
    assert refreshed is not None
    assert refreshed.status == "failed"
    assert refreshed.last_error_code == "invalid_recipient"
    # Referral should have a delivery_failed event now.
    events = storage.list_referral_events(scope, referral.id, limit=20)
    assert any(e.event_type == "delivery_failed" for e in events)


def test_process_one_channel_disabled_marks_failed_no_retry(storage: Storage):
    user_id = storage.create_user("a@example.com", "hashed")
    scope, _, referral = _seed(storage, user_id)
    d = storage.create_delivery(
        scope, referral_id=referral.id, channel="fax", recipient="+14155551234"
    )

    def _factory(name):
        raise ChannelDisabledError(name, reason="not configured")

    asyncio.run(
        _process_one(
            storage,
            d,
            channel_factory=_factory,
            render_packet=_noop_render,
        )
    )
    refreshed = storage.get_delivery(scope, d.id)
    assert refreshed is not None
    assert refreshed.status == "failed"  # not retried
    assert refreshed.last_error_code == "channel_disabled"


# ---------- Domain invariants ----------


def test_terminal_and_pickup_statuses_are_disjoint():
    assert TERMINAL_DELIVERY_STATUSES.isdisjoint(PICKUP_DELIVERY_STATUSES)


def test_all_status_values_known():
    assert set(TERMINAL_DELIVERY_STATUSES) <= set(DELIVERY_STATUS_VALUES)
    assert set(PICKUP_DELIVERY_STATUSES) <= set(DELIVERY_STATUS_VALUES)
