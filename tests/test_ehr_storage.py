"""Storage-level tests for ehr_connections (SQLite)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


def _make_connection(storage, user_id, **overrides):
    base = dict(
        user_id=user_id,
        ehr_vendor="epic_sandbox",
        iss="https://fake-epic.test/api/FHIR/R4",
        access_token_enc="AT_ENC",
        refresh_token_enc="RT_ENC",
        expires_at=datetime.now(tz=timezone.utc) + timedelta(hours=1),
        scope="openid",
        patient_fhir_id="PAT-1",
    )
    base.update(overrides)
    return storage.create_ehr_connection(**base)


def test_create_and_get_active(storage):
    user_id = storage.create_user("a@example.com", "pw")
    conn = _make_connection(storage, user_id)
    assert conn.id > 0
    assert conn.is_active()
    fetched = storage.get_active_ehr_connection(user_id, "epic_sandbox")
    assert fetched is not None
    assert fetched.id == conn.id


def test_create_revokes_prior_active(storage):
    """One active connection per (user, vendor) — race-safe revoke."""
    user_id = storage.create_user("a@example.com", "pw")
    first = _make_connection(storage, user_id, access_token_enc="AT1")
    second = _make_connection(storage, user_id, access_token_enc="AT2")

    # First should now be revoked, second is active.
    active = storage.get_active_ehr_connection(user_id, "epic_sandbox")
    assert active is not None
    assert active.id == second.id

    # Confirm prior is revoked at row level.
    rows = storage._conn.execute(
        "SELECT id, revoked_at FROM ehr_connections WHERE user_id = ? ORDER BY id",
        (user_id,),
    ).fetchall()
    assert rows[0]["id"] == first.id and rows[0]["revoked_at"] is not None
    assert rows[1]["id"] == second.id and rows[1]["revoked_at"] is None


def test_revoke_takes_user_and_vendor(storage):
    user_id = storage.create_user("a@example.com", "pw")
    _make_connection(storage, user_id)
    n = storage.revoke_ehr_connection(user_id, "epic_sandbox")
    assert n == 1
    assert storage.get_active_ehr_connection(user_id, "epic_sandbox") is None
    # Idempotent on no-active rows.
    assert storage.revoke_ehr_connection(user_id, "epic_sandbox") == 0


def test_revoke_isolated_per_vendor_user(storage):
    """Revoking one user's connection doesn't touch another's."""
    u1 = storage.create_user("a@example.com", "pw")
    u2 = storage.create_user("b@example.com", "pw")
    _make_connection(storage, u1)
    _make_connection(storage, u2)
    storage.revoke_ehr_connection(u1, "epic_sandbox")
    assert storage.get_active_ehr_connection(u1, "epic_sandbox") is None
    assert storage.get_active_ehr_connection(u2, "epic_sandbox") is not None


def test_update_tokens_replaces_only_token_fields(storage):
    user_id = storage.create_user("a@example.com", "pw")
    conn = _make_connection(storage, user_id)
    new_expires = datetime.now(tz=timezone.utc) + timedelta(hours=2)
    updated = storage.update_ehr_connection_tokens(
        conn.id,
        access_token_enc="AT_NEW",
        refresh_token_enc="RT_NEW",
        expires_at=new_expires,
    )
    assert updated.access_token_enc == "AT_NEW"
    assert updated.refresh_token_enc == "RT_NEW"
    assert updated.iss == conn.iss  # preserved
    assert updated.patient_fhir_id == conn.patient_fhir_id


def test_update_unknown_id_raises(storage):
    with pytest.raises(ValueError):
        storage.update_ehr_connection_tokens(
            99999,
            access_token_enc="x",
            refresh_token_enc=None,
            expires_at=datetime.now(tz=timezone.utc),
        )


def test_user_delete_cascades(storage):
    user_id = storage.create_user("a@example.com", "pw")
    _make_connection(storage, user_id)
    storage.delete_user(user_id)
    rows = storage._conn.execute(
        "SELECT id FROM ehr_connections WHERE user_id = ?", (user_id,)
    ).fetchall()
    assert rows == []
