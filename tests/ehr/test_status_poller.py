"""Issue #157 — EHR status poller orchestration tests.

Drives ``_process_one`` and ``_run_iteration`` directly so we don't need
the lifespan loop or a real httpx round-trip. Vendor read_service_request
calls are monkeypatched in the registry.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from docstats.ehr import ServiceRequestSnapshot
from docstats.ehr import registry as _ehr_registry
from docstats.ehr.registry import EHRError
from docstats.ehr.status_poller import (
    _process_one,
    _run_iteration,
    get_poll_stats,
)
from docstats.scope import Scope
from docstats.storage import Storage


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    return Storage(db_path=tmp_path / "t.db")


_user_counter = {"n": 0}


def _create_user_and_referral(
    s: Storage, *, vendor: str | None, sr_id: str | None, conn_id: int | None
):
    """Build a minimal user → patient → referral row and return the referral.

    Optionally pre-populates the issue-#157 write-back columns.
    """
    _user_counter["n"] += 1
    user_id = s.create_user(
        email=f"u{_user_counter['n']}@x.test",
        password_hash="fake-hash",
    )
    scope = Scope(user_id=user_id, organization_id=None, membership_role=None)
    patient = s.create_patient(scope, first_name="A", last_name="B")
    referral = s.create_referral(
        scope,
        patient_id=patient.id,
        created_by_user_id=user_id,
    )
    if sr_id and vendor:
        s.set_referral_ehr_writeback(
            referral.id,
            ehr_service_request_id=sr_id,
            ehr_vendor=vendor,
            ehr_connection_id=conn_id,
        )
    return s.get_referral(scope, referral.id), user_id, scope


def _stub_vendor(monkeypatch, vendor_name: str, snapshot_or_exc):
    """Replace registry[vendor_name].read_service_request with a stub.

    ``snapshot_or_exc`` is either a ServiceRequestSnapshot to return, or
    an Exception subclass instance to raise. Uses ``monkeypatch.setitem`` so
    the original module is restored at fixture teardown — otherwise the
    SimpleNamespace stub leaks into other tests that rely on the real
    vendor module (PKCE helpers, discovery, etc.).
    """

    def fake_read(*, access_token, service_request_id, **kw):
        if isinstance(snapshot_or_exc, BaseException):
            raise snapshot_or_exc
        return snapshot_or_exc

    mod = SimpleNamespace(read_service_request=fake_read)
    monkeypatch.setitem(_ehr_registry._REGISTRY, vendor_name, mod)


def _stub_token_resolver(monkeypatch, *, token: str | None = "T", err: str | None = None):
    from docstats.ehr import status_poller as sp

    monkeypatch.setattr(sp, "_resolve_access_token", lambda s, r: (token, None, err))


# ---------------------------------------------------------------------------
# _process_one
# ---------------------------------------------------------------------------


def test_process_one_writes_status_and_emits_change_event(storage, monkeypatch):
    referral, user, scope = _create_user_and_referral(
        storage, vendor="epic_sandbox", sr_id="SR-A", conn_id=None
    )
    _stub_vendor(
        monkeypatch,
        "epic_sandbox",
        ServiceRequestSnapshot(status="active", raw_status="active", last_modified=None),
    )
    _stub_token_resolver(monkeypatch)

    now = datetime.now(tz=timezone.utc)
    ok, changed = _process_one(storage, referral, now)
    assert ok is True
    assert changed is True

    fresh = storage.get_referral(scope, referral.id)
    assert fresh.ehr_status == "active"
    assert fresh.ehr_status_polled_at is not None
    assert fresh.ehr_status_error is None

    events = storage.list_referral_events(scope, referral.id)
    ehr_events = [e for e in events if e.event_type == "ehr_status"]
    assert len(ehr_events) == 1
    assert ehr_events[0].from_value is None
    assert ehr_events[0].to_value == "active"


def test_process_one_no_event_when_status_unchanged(storage, monkeypatch):
    referral, user, scope = _create_user_and_referral(
        storage, vendor="epic_sandbox", sr_id="SR-B", conn_id=None
    )
    # Seed prior ehr_status so the snapshot value is a no-op.
    storage.update_referral_ehr_status(
        referral.id, ehr_status="active", polled_at=datetime.now(tz=timezone.utc), error=None
    )
    referral = storage.get_referral(scope, referral.id)
    _stub_vendor(
        monkeypatch,
        "epic_sandbox",
        ServiceRequestSnapshot(status="active", raw_status="active", last_modified=None),
    )
    _stub_token_resolver(monkeypatch)

    now = datetime.now(tz=timezone.utc)
    ok, changed = _process_one(storage, referral, now)
    assert ok is True
    assert changed is False

    events = storage.list_referral_events(scope, referral.id)
    assert [e for e in events if e.event_type == "ehr_status"] == []


def test_process_one_ehr_error_stashes_message_but_keeps_status(storage, monkeypatch):
    referral, user, scope = _create_user_and_referral(
        storage, vendor="epic_sandbox", sr_id="SR-C", conn_id=None
    )
    storage.update_referral_ehr_status(
        referral.id, ehr_status="active", polled_at=datetime.now(tz=timezone.utc), error=None
    )
    referral = storage.get_referral(scope, referral.id)
    _stub_vendor(monkeypatch, "epic_sandbox", EHRError("Epic ServiceRequest.read returned 500"))
    _stub_token_resolver(monkeypatch)

    now = datetime.now(tz=timezone.utc)
    ok, changed = _process_one(storage, referral, now)
    assert ok is True
    assert changed is False

    fresh = storage.get_referral(scope, referral.id)
    assert fresh.ehr_status == "active"  # unchanged
    assert fresh.ehr_status_error is not None
    assert "500" in fresh.ehr_status_error


def test_process_one_skips_rows_without_writeback(storage, monkeypatch):
    # Build a fresh referral row WITHOUT setting writeback columns.
    referral, user, scope = _create_user_and_referral(
        storage, vendor=None, sr_id=None, conn_id=None
    )
    _stub_token_resolver(monkeypatch)

    now = datetime.now(tz=timezone.utc)
    ok, changed = _process_one(storage, referral, now)
    # Returns (False, False) without touching storage.
    assert ok is False
    assert changed is False


def test_process_one_auth_error_records_error(storage, monkeypatch):
    referral, user, scope = _create_user_and_referral(
        storage, vendor="epic_sandbox", sr_id="SR-D", conn_id=None
    )
    _stub_token_resolver(
        monkeypatch, token=None, err="epic_sandbox: connection_id missing on referral"
    )

    now = datetime.now(tz=timezone.utc)
    ok, changed = _process_one(storage, referral, now)
    assert ok is True
    assert changed is False

    fresh = storage.get_referral(scope, referral.id)
    assert fresh.ehr_status_error is not None
    assert "connection_id missing" in fresh.ehr_status_error


# ---------------------------------------------------------------------------
# _run_iteration
# ---------------------------------------------------------------------------


def test_run_iteration_processes_pollable_row(storage, monkeypatch):
    """End-to-end one-tick smoke: queue→fetch→vendor read→persist→change event.

    Single-row scenario sidesteps SQLite thread contention in the test runner;
    the per-row branches (change / no-change / error / auth-error) are covered
    by the direct ``_process_one`` tests above.
    """
    r, _user_id, scope = _create_user_and_referral(
        storage, vendor="epic_sandbox", sr_id="SR-1", conn_id=None
    )
    _stub_vendor(
        monkeypatch,
        "epic_sandbox",
        ServiceRequestSnapshot(status="completed", raw_status="completed", last_modified=None),
    )
    _stub_token_resolver(monkeypatch)

    processed, errors, changed = asyncio.run(_run_iteration(storage))
    assert processed == 1
    assert changed == 1
    assert errors == 0

    fresh = storage.get_referral(scope, r.id)
    assert fresh.ehr_status == "completed"


def test_run_iteration_no_rows_short_circuits(storage):
    # No referrals in the DB at all — list returns empty, loop returns zeros.
    processed, errors, changed = asyncio.run(_run_iteration(storage))
    assert (processed, errors, changed) == (0, 0, 0)


# ---------------------------------------------------------------------------
# get_poll_stats
# ---------------------------------------------------------------------------


def test_get_poll_stats_returns_snapshot():
    snap = get_poll_stats()
    assert snap.running is False or snap.running is True  # bool either way
    assert isinstance(snap.total_iterations, int)
