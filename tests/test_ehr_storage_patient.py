"""Storage-level tests for patient-scoped EHR connections (Issue #155)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from docstats.scope import Scope


def _mk_patient(storage, user_id: int, *, mrn: str = "MRN-1") -> int:
    p = storage.create_patient(
        Scope(user_id=user_id),
        first_name="Kid",
        last_name="Doe",
        mrn=mrn,
        relationship="child",
        ehr_fhir_id="EPIC-PAT-CHILD",
    )
    return p.id


def _mk_patient_conn(storage, patient_id: int, **overrides):
    base = dict(
        patient_id=patient_id,
        ehr_vendor="epic_sandbox",
        iss="https://fake-epic.test/api/FHIR/R4",
        access_token_enc="AT_ENC",
        refresh_token_enc="RT_ENC",
        expires_at=datetime.now(tz=timezone.utc) + timedelta(hours=1),
        scope="openid",
        patient_fhir_id="EPIC-PAT-CHILD",
    )
    base.update(overrides)
    return storage.create_patient_ehr_connection(**base)


def test_create_and_get_active_patient_scoped(storage):
    user_id = storage.create_user("parent@example.com", "pw")
    patient_id = _mk_patient(storage, user_id)
    conn = _mk_patient_conn(storage, patient_id)
    assert conn.id > 0
    assert conn.patient_id == patient_id
    assert conn.user_id is None
    assert conn.organization_id is None
    assert conn.is_patient_scoped
    fetched = storage.get_active_patient_ehr_connection(patient_id, "epic_sandbox")
    assert fetched is not None and fetched.id == conn.id


def test_create_revokes_prior_active_patient(storage):
    user_id = storage.create_user("parent@example.com", "pw")
    patient_id = _mk_patient(storage, user_id)
    first = _mk_patient_conn(storage, patient_id, access_token_enc="AT1")
    second = _mk_patient_conn(storage, patient_id, access_token_enc="AT2")

    active = storage.get_active_patient_ehr_connection(patient_id, "epic_sandbox")
    assert active is not None and active.id == second.id

    rows = storage._conn.execute(
        "SELECT id, revoked_at FROM ehr_connections WHERE patient_id = ? ORDER BY id",
        (patient_id,),
    ).fetchall()
    assert rows[0]["id"] == first.id and rows[0]["revoked_at"] is not None
    assert rows[1]["id"] == second.id and rows[1]["revoked_at"] is None


def test_revoke_patient_connection_idempotent(storage):
    user_id = storage.create_user("parent@example.com", "pw")
    patient_id = _mk_patient(storage, user_id)
    _mk_patient_conn(storage, patient_id)
    assert storage.revoke_patient_ehr_connection(patient_id, "epic_sandbox") == 1
    assert storage.get_active_patient_ehr_connection(patient_id, "epic_sandbox") is None
    # Idempotent on no-active rows.
    assert storage.revoke_patient_ehr_connection(patient_id, "epic_sandbox") == 0


def test_patient_delete_cascades_to_connection(storage):
    user_id = storage.create_user("parent@example.com", "pw")
    patient_id = _mk_patient(storage, user_id)
    _mk_patient_conn(storage, patient_id)
    storage._conn.execute("DELETE FROM patients WHERE id = ?", (patient_id,))
    storage._conn.commit()
    rows = storage._conn.execute(
        "SELECT id FROM ehr_connections WHERE patient_id = ?", (patient_id,)
    ).fetchall()
    assert rows == []


def test_patient_and_user_connections_coexist(storage):
    """A parent can hold their own user-scoped Epic connection plus a
    separate patient-scoped Epic connection for a dependent without
    either CRUD path interfering with the other."""
    user_id = storage.create_user("parent@example.com", "pw")
    patient_id = _mk_patient(storage, user_id)

    parent_conn = storage.create_ehr_connection(
        user_id=user_id,
        ehr_vendor="epic_sandbox",
        iss="https://fake-epic.test/api/FHIR/R4",
        access_token_enc="PARENT_AT",
        refresh_token_enc="PARENT_RT",
        expires_at=datetime.now(tz=timezone.utc) + timedelta(hours=1),
        scope="openid",
        patient_fhir_id="EPIC-PAT-PARENT",
    )
    child_conn = _mk_patient_conn(storage, patient_id, access_token_enc="CHILD_AT")

    # Each lookup sees only its own scope.
    parent_view = storage.get_active_ehr_connection(user_id, "epic_sandbox")
    assert parent_view is not None and parent_view.id == parent_conn.id
    child_view = storage.get_active_patient_ehr_connection(patient_id, "epic_sandbox")
    assert child_view is not None and child_view.id == child_conn.id

    # Revoking the parent's row leaves the child's untouched.
    storage.revoke_ehr_connection(user_id, "epic_sandbox")
    assert storage.get_active_ehr_connection(user_id, "epic_sandbox") is None
    assert storage.get_active_patient_ehr_connection(patient_id, "epic_sandbox") is not None


def test_owner_xor_blocks_multi_owner_inserts(storage):
    """The 3-way CHECK constraint rejects rows with more than one owner set."""
    user_id = storage.create_user("parent@example.com", "pw")
    patient_id = _mk_patient(storage, user_id)
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        # Manually bypass the storage layer to test the DB-level guard.
        storage._conn.execute(
            "INSERT INTO ehr_connections"
            " (user_id, organization_id, patient_id, ehr_vendor, iss,"
            "  created_at, updated_at)"
            " VALUES (?, NULL, ?, 'epic_sandbox', 'iss', datetime('now'), datetime('now'))",
            (user_id, patient_id),
        )


def test_partial_unique_index_blocks_two_active_patient_rows(storage):
    """Only one active connection per (patient, vendor); historical revoked
    rows are fine. The race-safe revoke inside create_patient_ehr_connection
    is what users actually invoke — exercise it end-to-end."""
    user_id = storage.create_user("parent@example.com", "pw")
    patient_id = _mk_patient(storage, user_id)
    _mk_patient_conn(storage, patient_id, access_token_enc="AT1")
    _mk_patient_conn(storage, patient_id, access_token_enc="AT2")
    rows = storage._conn.execute(
        "SELECT id FROM ehr_connections WHERE patient_id = ? AND revoked_at IS NULL",
        (patient_id,),
    ).fetchall()
    assert len(rows) == 1
