"""Phase 10.C — Attachment retention storage + sweep tests."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from docstats.domain.orgs import (
    DEFAULT_ATTACHMENT_RETENTION_DAYS,
    MAX_ATTACHMENT_RETENTION_DAYS,
    MIN_ATTACHMENT_RETENTION_DAYS,
)
from docstats.scope import Scope
from docstats.storage import Storage
from docstats.storage_files import InMemoryFileBackend
from docstats.storage_files.retention import (
    DEFAULT_INTERVAL_SECONDS,
    _get_interval_seconds,
    get_retention_stats,
    run_sweep,
)


# ---------- Fixtures ----------


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    return Storage(db_path=tmp_path / "retention.db")


def _org_scope(storage: Storage, *, retention_days: int | None = None):
    org = storage.create_organization(name="Acme", slug="acme")
    if retention_days is not None:
        storage.update_organization(
            org.id, attachment_retention_days=retention_days, name="Acme", overwrite=True
        )
        org = storage.get_organization(org.id)
    user_id = storage.create_user("a@example.com", "h")
    storage.create_membership(organization_id=org.id, user_id=user_id, role="admin")
    return org, Scope(organization_id=org.id, membership_role="admin"), user_id


def _seed_attachment(
    storage: Storage,
    scope: Scope,
    user_id: int,
    *,
    storage_ref: str | None = "org-1/1/1.pdf",
    created_at_override: datetime | None = None,
):
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
    att = storage.add_referral_attachment(
        scope,
        referral.id,
        kind="lab",
        label="x",
        storage_ref=storage_ref,
        checklist_only=False,
    )
    if created_at_override is not None:
        storage._conn.execute(
            "UPDATE referral_attachments SET created_at = ? WHERE id = ?",
            (created_at_override.strftime("%Y-%m-%d %H:%M:%S"), att.id),
        )
        storage._conn.commit()
    return referral, att


# ---------- Organization.attachment_retention_days ----------


def test_org_defaults_to_7_years(storage: Storage) -> None:
    org = storage.create_organization(name="X", slug="x")
    assert org.attachment_retention_days == DEFAULT_ATTACHMENT_RETENTION_DAYS


def test_org_update_retention_days(storage: Storage) -> None:
    org = storage.create_organization(name="X", slug="x")
    updated = storage.update_organization(
        org.id,
        name="X",
        attachment_retention_days=365,
        overwrite=True,
    )
    assert updated is not None
    assert updated.attachment_retention_days == 365


def test_org_retention_bounds_enforced(storage: Storage) -> None:
    org = storage.create_organization(name="X", slug="x")
    with pytest.raises(ValueError, match="between"):
        storage.update_organization(org.id, attachment_retention_days=10, name="X", overwrite=True)
    with pytest.raises(ValueError, match="between"):
        storage.update_organization(
            org.id, attachment_retention_days=999_999, name="X", overwrite=True
        )


def test_retention_min_and_max_constants() -> None:
    assert MIN_ATTACHMENT_RETENTION_DAYS < DEFAULT_ATTACHMENT_RETENTION_DAYS
    assert DEFAULT_ATTACHMENT_RETENTION_DAYS < MAX_ATTACHMENT_RETENTION_DAYS


# ---------- list_attachments_expired ----------


def test_list_expired_empty(storage: Storage) -> None:
    _, scope, user_id = _org_scope(storage)
    cutoff = datetime.now(tz=timezone.utc)
    rows = storage.list_attachments_expired(cutoff, scope_organization_id=scope.organization_id)
    assert rows == []


def test_list_expired_returns_old_rows_only(storage: Storage) -> None:
    _, scope, user_id = _org_scope(storage)
    # Row A — created an hour ago (fresh).
    _seed_attachment(storage, scope, user_id, storage_ref="x/a.pdf")
    # Row B — created 10 days ago.
    old = datetime.now(tz=timezone.utc) - timedelta(days=10)
    _, old_att = _seed_attachment(
        storage, scope, user_id, storage_ref="x/b.pdf", created_at_override=old
    )
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=5)
    rows = storage.list_attachments_expired(cutoff, scope_organization_id=scope.organization_id)
    assert [r.id for r in rows] == [old_att.id]


def test_list_expired_skips_rows_without_storage_ref(storage: Storage) -> None:
    """Pre-10.A placeholder rows (checklist_only, no storage_ref) are not purge targets."""
    _, scope, user_id = _org_scope(storage)
    old = datetime.now(tz=timezone.utc) - timedelta(days=10)
    _seed_attachment(storage, scope, user_id, storage_ref=None, created_at_override=old)
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=5)
    rows = storage.list_attachments_expired(cutoff, scope_organization_id=scope.organization_id)
    assert rows == []


def test_list_expired_requires_exactly_one_scope(storage: Storage) -> None:
    cutoff = datetime.now(tz=timezone.utc)
    with pytest.raises(ValueError):
        storage.list_attachments_expired(cutoff)
    with pytest.raises(ValueError):
        storage.list_attachments_expired(cutoff, scope_organization_id=1, scope_user_id=1)


def test_list_expired_scope_isolation(storage: Storage) -> None:
    """Org A's query must not return org B's expired attachments."""
    _, scope_a, user_a = _org_scope(storage)
    org_b = storage.create_organization(name="B", slug="b")
    user_b = storage.create_user("b@example.com", "h")
    storage.create_membership(organization_id=org_b.id, user_id=user_b, role="admin")
    scope_b = Scope(organization_id=org_b.id, membership_role="admin")
    old = datetime.now(tz=timezone.utc) - timedelta(days=10)
    _seed_attachment(storage, scope_a, user_a, storage_ref="a/1.pdf", created_at_override=old)
    _, att_b = _seed_attachment(
        storage, scope_b, user_b, storage_ref="b/1.pdf", created_at_override=old
    )
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=5)
    rows_a = storage.list_attachments_expired(cutoff, scope_organization_id=scope_a.organization_id)
    rows_b = storage.list_attachments_expired(cutoff, scope_organization_id=org_b.id)
    assert all(r.id != att_b.id for r in rows_a)
    assert [r.id for r in rows_b] == [att_b.id]


def test_list_expired_solo_scope(storage: Storage) -> None:
    """Solo-user scope works symmetrically."""
    user_id = storage.create_user("solo@example.com", "h")
    scope = Scope(user_id=user_id)
    old = datetime.now(tz=timezone.utc) - timedelta(days=10)
    _, att = _seed_attachment(
        storage, scope, user_id, storage_ref="user-1/1.pdf", created_at_override=old
    )
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=5)
    rows = storage.list_attachments_expired(cutoff, scope_user_id=user_id)
    assert [r.id for r in rows] == [att.id]


# ---------- list_solo_user_ids_with_attachments ----------


def test_list_solo_users_empty(storage: Storage) -> None:
    assert storage.list_solo_user_ids_with_attachments() == []


def test_list_solo_users_returns_owners(storage: Storage) -> None:
    user_a = storage.create_user("a@example.com", "h")
    user_b = storage.create_user("b@example.com", "h")
    _seed_attachment(storage, Scope(user_id=user_a), user_a, storage_ref="a/1.pdf")
    _seed_attachment(storage, Scope(user_id=user_b), user_b, storage_ref="b/1.pdf")
    ids = storage.list_solo_user_ids_with_attachments()
    assert set(ids) == {user_a, user_b}


def test_list_solo_users_excludes_org_owners(storage: Storage) -> None:
    """Attachments owned by an org must not surface as solo users."""
    _, scope, uid = _org_scope(storage)
    _seed_attachment(storage, scope, uid, storage_ref="org/1.pdf")
    assert storage.list_solo_user_ids_with_attachments() == []


def test_list_solo_users_excludes_no_bytes_rows(storage: Storage) -> None:
    user_id = storage.create_user("a@example.com", "h")
    _seed_attachment(storage, Scope(user_id=user_id), user_id, storage_ref=None)
    assert storage.list_solo_user_ids_with_attachments() == []


# ---------- list_all_organizations ----------


def test_list_all_organizations(storage: Storage) -> None:
    storage.create_organization(name="A", slug="a")
    b = storage.create_organization(name="B", slug="b")
    storage.soft_delete_organization(b.id)
    ids = [o.id for o in storage.list_all_organizations()]
    assert b.id not in ids
    with_deleted = [o.id for o in storage.list_all_organizations(include_deleted=True)]
    assert b.id in with_deleted


# ---------- run_sweep ----------


def test_sweep_purges_expired_org_attachments(storage: Storage) -> None:
    org, scope, user_id = _org_scope(storage, retention_days=30)
    backend = InMemoryFileBackend()
    old = datetime.now(tz=timezone.utc) - timedelta(days=60)
    _, att = _seed_attachment(
        storage, scope, user_id, storage_ref="x/1.pdf", created_at_override=old
    )
    asyncio.run(backend.put(path="x/1.pdf", data=b"pdf", mime_type="application/pdf"))

    purged = asyncio.run(run_sweep(storage, backend))
    assert purged == 1
    # DB row gone + bucket purged + audit recorded.
    assert storage.get_referral_attachment(scope, att.id) is None
    assert not backend._has("x/1.pdf")
    events = storage.list_audit_events(scope_organization_id=org.id, action="attachment.purged")
    assert len(events) == 1


def test_sweep_keeps_fresh_attachments(storage: Storage) -> None:
    org, scope, user_id = _org_scope(storage, retention_days=30)
    backend = InMemoryFileBackend()
    _, att = _seed_attachment(storage, scope, user_id, storage_ref="fresh.pdf")
    asyncio.run(backend.put(path="fresh.pdf", data=b"x", mime_type="application/pdf"))

    purged = asyncio.run(run_sweep(storage, backend))
    assert purged == 0
    assert storage.get_referral_attachment(scope, att.id) is not None
    assert backend._has("fresh.pdf")


def test_sweep_respects_per_org_retention(storage: Storage) -> None:
    """Two orgs, different retention_days — each uses its own cutoff."""
    org_short, scope_short, uid_s = _org_scope(storage, retention_days=30)
    # Second org: build directly (helper creates unique slug via org_scope only once)
    org_long = storage.create_organization(name="Long", slug="long")
    storage.update_organization(
        org_long.id, name="Long", attachment_retention_days=365, overwrite=True
    )
    user_l = storage.create_user("l@example.com", "h")
    storage.create_membership(organization_id=org_long.id, user_id=user_l, role="admin")
    scope_long = Scope(organization_id=org_long.id, membership_role="admin")

    backend = InMemoryFileBackend()
    old = datetime.now(tz=timezone.utc) - timedelta(days=60)
    _, att_s = _seed_attachment(
        storage, scope_short, uid_s, storage_ref="s/1.pdf", created_at_override=old
    )
    _, att_l = _seed_attachment(
        storage, scope_long, user_l, storage_ref="l/1.pdf", created_at_override=old
    )
    for ref in ("s/1.pdf", "l/1.pdf"):
        asyncio.run(backend.put(path=ref, data=b"x", mime_type="application/pdf"))

    asyncio.run(run_sweep(storage, backend))
    assert storage.get_referral_attachment(scope_short, att_s.id) is None
    assert storage.get_referral_attachment(scope_long, att_l.id) is not None


def test_sweep_handles_solo_users(storage: Storage) -> None:
    user_id = storage.create_user("solo@example.com", "h")
    scope = Scope(user_id=user_id)
    backend = InMemoryFileBackend()
    # Old attachment — solo users use the platform default (7 years).  Back-date
    # well beyond that so it purges.
    old = datetime.now(tz=timezone.utc) - timedelta(days=DEFAULT_ATTACHMENT_RETENTION_DAYS + 10)
    _, att = _seed_attachment(
        storage, scope, user_id, storage_ref="u/1.pdf", created_at_override=old
    )
    asyncio.run(backend.put(path="u/1.pdf", data=b"x", mime_type="application/pdf"))

    purged = asyncio.run(run_sweep(storage, backend))
    assert purged == 1
    assert storage.get_referral_attachment(scope, att.id) is None


def test_sweep_bucket_failure_still_removes_db_row(storage: Storage) -> None:
    """If the bucket delete raises, we still drop the DB row (orphan bytes
    become the next sweep's problem; the audit trail records the purge)."""
    from docstats.storage_files.base import StorageFileError

    _, scope, user_id = _org_scope(storage, retention_days=30)
    old = datetime.now(tz=timezone.utc) - timedelta(days=60)
    _, att = _seed_attachment(
        storage, scope, user_id, storage_ref="boom.pdf", created_at_override=old
    )

    class _BoomBackend(InMemoryFileBackend):
        async def delete(self, path):
            raise StorageFileError("bucket down")

    backend = _BoomBackend()
    purged = asyncio.run(run_sweep(storage, backend))
    assert purged == 1
    assert storage.get_referral_attachment(scope, att.id) is None


def test_sweep_continues_when_one_tenant_fails(storage: Storage) -> None:
    """A storage failure on org A must not block org B's purge."""
    org_a, scope_a, uid_a = _org_scope(storage, retention_days=30)
    org_b = storage.create_organization(name="B", slug="b")
    storage.update_organization(org_b.id, name="B", attachment_retention_days=30, overwrite=True)
    user_b = storage.create_user("b@example.com", "h")
    storage.create_membership(organization_id=org_b.id, user_id=user_b, role="admin")
    scope_b = Scope(organization_id=org_b.id, membership_role="admin")

    backend = InMemoryFileBackend()
    old = datetime.now(tz=timezone.utc) - timedelta(days=60)
    _seed_attachment(storage, scope_a, uid_a, storage_ref="a/1.pdf", created_at_override=old)
    _, att_b = _seed_attachment(
        storage, scope_b, user_b, storage_ref="b/1.pdf", created_at_override=old
    )
    asyncio.run(backend.put(path="b/1.pdf", data=b"x", mime_type="application/pdf"))

    # Monkeypatch the expired query to blow up for org A only.
    original = storage.list_attachments_expired

    def _selective_boom(cutoff, *, scope_organization_id=None, scope_user_id=None, limit=500):
        if scope_organization_id == org_a.id:
            raise RuntimeError("simulated A outage")
        return original(
            cutoff,
            scope_organization_id=scope_organization_id,
            scope_user_id=scope_user_id,
            limit=limit,
        )

    storage.list_attachments_expired = _selective_boom  # type: ignore[method-assign]
    try:
        asyncio.run(run_sweep(storage, backend))
    finally:
        storage.list_attachments_expired = original  # type: ignore[method-assign]

    # Org B's attachment was still purged.
    assert storage.get_referral_attachment(scope_b, att_b.id) is None


# ---------- Stats snapshot ----------


def test_retention_stats_snapshot_is_independent() -> None:
    s1 = get_retention_stats()
    s1.total_purged = 999
    s2 = get_retention_stats()
    assert s2.total_purged != 999


# ---------- Env var parsing ----------


def test_interval_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ATTACHMENT_RETENTION_INTERVAL_SECONDS", raising=False)
    assert _get_interval_seconds() == DEFAULT_INTERVAL_SECONDS


def test_interval_clamped_low(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATTACHMENT_RETENTION_INTERVAL_SECONDS", "5")
    # Floor is 60s.
    assert _get_interval_seconds() == 60


def test_interval_clamped_high(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATTACHMENT_RETENTION_INTERVAL_SECONDS", "99999999")
    # Ceiling is 7 days.
    assert _get_interval_seconds() == 7 * 86400


# ---------- Admin form integration (covered by test_admin_org_settings) ----------
# The admin form's validation path lives in test_admin_org_settings.py; this
# module keeps its focus on storage + sweep logic.
