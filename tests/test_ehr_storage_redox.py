"""Storage-level tests for org-scoped EHR connections (Phase 12.E Redox)."""

from __future__ import annotations

import pytest


def _new_org(storage, slug: str = "acme-clinic") -> int:
    org = storage.create_organization(name="Acme Clinic", slug=slug)
    return org.id


def test_create_org_ehr_connection_persists(storage):
    org_id = _new_org(storage)
    conn = storage.create_org_ehr_connection(
        organization_id=org_id,
        ehr_vendor="redox",
        iss="redox-fhir-sandbox/Development",
        scope="system/Patient.read",
    )
    assert conn.id > 0
    assert conn.organization_id == org_id
    assert conn.user_id is None
    assert conn.is_org_scoped is True
    assert conn.access_token_enc is None
    assert conn.expires_at is None


def test_get_active_org_connection_returns_latest(storage):
    org_id = _new_org(storage)
    storage.create_org_ehr_connection(
        organization_id=org_id, ehr_vendor="redox", iss="dest-A/Development"
    )
    second = storage.create_org_ehr_connection(
        organization_id=org_id, ehr_vendor="redox", iss="dest-B/Development"
    )
    active = storage.get_active_org_ehr_connection(org_id, "redox")
    assert active is not None
    assert active.id == second.id
    assert active.iss == "dest-B/Development"


def test_create_revokes_prior_active_org_connection(storage):
    org_id = _new_org(storage)
    first = storage.create_org_ehr_connection(
        organization_id=org_id, ehr_vendor="redox", iss="dest-A/Development"
    )
    storage.create_org_ehr_connection(
        organization_id=org_id, ehr_vendor="redox", iss="dest-B/Development"
    )
    rows = storage._conn.execute(
        "SELECT id, revoked_at FROM ehr_connections "
        "WHERE organization_id = ? ORDER BY id",
        (org_id,),
    ).fetchall()
    assert len(rows) == 2
    assert rows[0]["id"] == first.id
    assert rows[0]["revoked_at"] is not None
    assert rows[1]["revoked_at"] is None


def test_revoke_org_ehr_connection_returns_count(storage):
    org_id = _new_org(storage)
    storage.create_org_ehr_connection(
        organization_id=org_id, ehr_vendor="redox", iss="dest/Development"
    )
    count = storage.revoke_org_ehr_connection(org_id, "redox")
    assert count == 1
    # Idempotent: second call revokes nothing.
    assert storage.revoke_org_ehr_connection(org_id, "redox") == 0
    assert storage.get_active_org_ehr_connection(org_id, "redox") is None


def test_list_active_org_ehr_connections_excludes_revoked(storage):
    org_id = _new_org(storage)
    storage.create_org_ehr_connection(
        organization_id=org_id, ehr_vendor="redox", iss="dest/Development"
    )
    storage.revoke_org_ehr_connection(org_id, "redox")
    assert storage.list_active_org_ehr_connections(org_id) == []
    # Re-create — should appear in the list.
    new = storage.create_org_ehr_connection(
        organization_id=org_id, ehr_vendor="redox", iss="dest2/Development"
    )
    actives = storage.list_active_org_ehr_connections(org_id)
    assert [c.id for c in actives] == [new.id]


def test_org_scoped_connection_isolated_from_user_scoped(storage):
    """A user-scoped Epic connection and an org-scoped Redox connection coexist."""
    from datetime import datetime, timedelta, timezone

    org_id = _new_org(storage)
    user_id = storage.create_user("u@example.com", "pw")
    storage.create_ehr_connection(
        user_id=user_id,
        ehr_vendor="epic_sandbox",
        iss="https://fake-epic.test/FHIR",
        access_token_enc="AT",
        refresh_token_enc=None,
        expires_at=datetime.now(tz=timezone.utc) + timedelta(hours=1),
        scope="openid",
        patient_fhir_id=None,
    )
    storage.create_org_ehr_connection(
        organization_id=org_id, ehr_vendor="redox", iss="dest/Development"
    )

    # Org listing returns only the org-scoped one.
    org_actives = storage.list_active_org_ehr_connections(org_id)
    assert len(org_actives) == 1
    assert org_actives[0].ehr_vendor == "redox"

    # User listing returns only the user-scoped one.
    user_actives = storage.list_active_ehr_connections(user_id)
    assert len(user_actives) == 1
    assert user_actives[0].ehr_vendor == "epic_sandbox"


def test_org_check_constraint_rejects_both_owners(storage):
    """SQLite CHECK enforces exactly-one-owner."""
    import sqlite3

    org_id = _new_org(storage)
    user_id = storage.create_user("u@example.com", "pw")
    with pytest.raises(sqlite3.IntegrityError):
        storage._conn.execute(
            "INSERT INTO ehr_connections "
            "(user_id, organization_id, ehr_vendor, iss, created_at, updated_at) "
            "VALUES (?, ?, 'redox', 'dest/Dev', datetime('now'), datetime('now'))",
            (user_id, org_id),
        )
        storage._conn.commit()


def test_org_partial_unique_index_blocks_duplicate_active(storage):
    """Two active rows for (org, vendor) violate the partial unique index."""
    import sqlite3

    org_id = _new_org(storage)
    storage.create_org_ehr_connection(
        organization_id=org_id, ehr_vendor="redox", iss="dest/Development"
    )
    # Bypass create_org_ehr_connection (which auto-revokes prior); attempt a
    # second raw insert with no revoked_at — must violate the partial UNIQUE.
    with pytest.raises(sqlite3.IntegrityError):
        storage._conn.execute(
            "INSERT INTO ehr_connections "
            "(organization_id, ehr_vendor, iss, created_at, updated_at) "
            "VALUES (?, 'redox', 'dup/Dev', datetime('now'), datetime('now'))",
            (org_id,),
        )
        storage._conn.commit()
