"""Storage-layer tests for organization invitations (Phase 6.F)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from docstats.domain.invitations import (
    DEFAULT_INVITATION_TTL_SECONDS,
    MAX_INVITATION_TTL_SECONDS,
    MIN_INVITATION_TTL_SECONDS,
    Invitation,
    compute_expires_at,
    generate_token,
    validate_role,
)
from docstats.storage import Storage


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    return Storage(db_path=tmp_path / "invites.db")


@pytest.fixture
def org_and_admin(storage: Storage):
    user_id = storage.create_user("admin@example.com", "hashed")
    org = storage.create_organization(name="Acme", slug="acme")
    storage.create_membership(organization_id=org.id, user_id=user_id, role="admin")
    return user_id, org


def _make(storage: Storage, org_id: int, admin_id: int, *, email: str = "new@example.com"):
    return storage.create_invitation(
        organization_id=org_id,
        email=email,
        role="staff",
        token=generate_token(),
        expires_at=compute_expires_at(DEFAULT_INVITATION_TTL_SECONDS),
        invited_by_user_id=admin_id,
    )


# --- Token + helper behavior ---


def test_generate_token_is_url_safe_and_long_enough() -> None:
    t = generate_token()
    assert len(t) >= 40  # 32 bytes → 43-ish base64url chars
    assert all(c.isalnum() or c in "-_" for c in t)


def test_compute_expires_at_clamps_below_minimum() -> None:
    now = datetime.now(tz=timezone.utc)
    exp = compute_expires_at(0)
    # Clamped to MIN.
    delta = (exp - now).total_seconds()
    assert delta >= MIN_INVITATION_TTL_SECONDS - 5
    assert delta <= MIN_INVITATION_TTL_SECONDS + 5


def test_compute_expires_at_clamps_above_maximum() -> None:
    now = datetime.now(tz=timezone.utc)
    exp = compute_expires_at(MAX_INVITATION_TTL_SECONDS * 10)
    delta = (exp - now).total_seconds()
    assert delta <= MAX_INVITATION_TTL_SECONDS + 5


def test_validate_role_accepts_known_roles() -> None:
    for r in ("owner", "admin", "coordinator", "clinician", "staff", "read_only"):
        assert validate_role(r) == r


def test_validate_role_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        validate_role("god")


# --- Storage CRUD ---


def test_create_round_trips(storage: Storage, org_and_admin) -> None:
    admin_id, org = org_and_admin
    inv = _make(storage, org.id, admin_id, email="NEW@example.com")
    assert inv.id > 0
    # Email lowercased at the storage boundary.
    assert inv.email == "new@example.com"
    assert inv.is_pending()

    fetched = storage.get_invitation_by_token(inv.token)
    assert fetched is not None
    assert fetched.id == inv.id

    by_id = storage.get_invitation(inv.id)
    assert by_id is not None
    assert by_id.id == inv.id


def test_get_by_unknown_token_returns_none(storage: Storage) -> None:
    assert storage.get_invitation_by_token("nope") is None


def test_list_pending_only_by_default(storage: Storage, org_and_admin) -> None:
    admin_id, org = org_and_admin
    _make(storage, org.id, admin_id, email="a@example.com")
    inv_b = _make(storage, org.id, admin_id, email="b@example.com")
    inv_c = _make(storage, org.id, admin_id, email="c@example.com")
    storage.revoke_invitation(inv_b.id)
    storage.mark_invitation_accepted(inv_c.id)

    pending = storage.list_invitations_for_org(org.id)
    assert [i.email for i in pending] == ["a@example.com"]

    with_revoked = storage.list_invitations_for_org(org.id, include_revoked=True)
    emails = {i.email for i in with_revoked}
    assert emails == {"a@example.com", "b@example.com"}

    with_accepted = storage.list_invitations_for_org(org.id, include_accepted=True)
    emails = {i.email for i in with_accepted}
    assert emails == {"a@example.com", "c@example.com"}

    with_all = storage.list_invitations_for_org(org.id, include_revoked=True, include_accepted=True)
    assert len(with_all) == 3


def test_list_excludes_expired_by_default(storage: Storage, org_and_admin) -> None:
    admin_id, org = org_and_admin
    # Insert an invitation with a past expires_at using direct SQL to
    # bypass the MIN TTL clamp.
    token = generate_token()
    past = datetime.now(tz=timezone.utc) - timedelta(hours=1)
    storage._conn.execute(
        "INSERT INTO organization_invitations (organization_id, email, role, token, expires_at) "
        "VALUES (?, ?, ?, ?, datetime(?))",
        (org.id, "old@example.com", "staff", token, past.strftime("%Y-%m-%d %H:%M:%S")),
    )
    storage._conn.commit()
    _make(storage, org.id, admin_id, email="fresh@example.com")

    pending = storage.list_invitations_for_org(org.id)
    assert [i.email for i in pending] == ["fresh@example.com"]

    with_expired = storage.list_invitations_for_org(org.id, include_expired=True)
    emails = {i.email for i in with_expired}
    assert emails == {"old@example.com", "fresh@example.com"}


def test_revoke_is_idempotent(storage: Storage, org_and_admin) -> None:
    admin_id, org = org_and_admin
    inv = _make(storage, org.id, admin_id)
    assert storage.revoke_invitation(inv.id) is True
    assert storage.revoke_invitation(inv.id) is False
    # Row still reachable by id, just marked revoked.
    fetched = storage.get_invitation(inv.id)
    assert fetched is not None
    assert fetched.revoked_at is not None
    assert not fetched.is_pending()


def test_revoke_missing_returns_false(storage: Storage) -> None:
    assert storage.revoke_invitation(9999) is False


def test_mark_accepted_refuses_revoked(storage: Storage, org_and_admin) -> None:
    admin_id, org = org_and_admin
    inv = _make(storage, org.id, admin_id)
    storage.revoke_invitation(inv.id)
    assert storage.mark_invitation_accepted(inv.id) is False


def test_mark_accepted_refuses_expired(storage: Storage, org_and_admin) -> None:
    admin_id, org = org_and_admin
    token = generate_token()
    past = datetime.now(tz=timezone.utc) - timedelta(hours=1)
    cursor = storage._conn.execute(
        "INSERT INTO organization_invitations (organization_id, email, role, token, expires_at) "
        "VALUES (?, ?, ?, ?, datetime(?))",
        (org.id, "x@example.com", "staff", token, past.strftime("%Y-%m-%d %H:%M:%S")),
    )
    storage._conn.commit()
    assert storage.mark_invitation_accepted(cursor.lastrowid) is False


def test_mark_accepted_happy_path(storage: Storage, org_and_admin) -> None:
    admin_id, org = org_and_admin
    inv = _make(storage, org.id, admin_id)
    assert storage.mark_invitation_accepted(inv.id) is True
    assert storage.mark_invitation_accepted(inv.id) is False  # idempotent
    fetched = storage.get_invitation(inv.id)
    assert fetched is not None
    assert fetched.accepted_at is not None


# --- Partial unique index (pending, per-email) ---


def test_duplicate_pending_invite_raises(storage: Storage, org_and_admin) -> None:
    admin_id, org = org_and_admin
    _make(storage, org.id, admin_id, email="same@example.com")
    with pytest.raises(Exception):
        _make(storage, org.id, admin_id, email="same@example.com")


def test_can_reinvite_after_revoke(storage: Storage, org_and_admin) -> None:
    admin_id, org = org_and_admin
    inv = _make(storage, org.id, admin_id, email="rotate@example.com")
    storage.revoke_invitation(inv.id)
    # Revoked row no longer matches the partial unique index → new
    # invitation should succeed.
    new_inv = _make(storage, org.id, admin_id, email="rotate@example.com")
    assert new_inv.id != inv.id


def test_can_reinvite_after_accept(storage: Storage, org_and_admin) -> None:
    admin_id, org = org_and_admin
    inv = _make(storage, org.id, admin_id, email="grew@example.com")
    storage.mark_invitation_accepted(inv.id)
    new_inv = _make(storage, org.id, admin_id, email="grew@example.com")
    assert new_inv.id != inv.id


# --- is_pending model method ---


def test_is_pending_false_after_accept(storage: Storage, org_and_admin) -> None:
    admin_id, org = org_and_admin
    inv = _make(storage, org.id, admin_id)
    storage.mark_invitation_accepted(inv.id)
    refetched = storage.get_invitation(inv.id)
    assert refetched is not None
    assert refetched.is_pending() is False


def test_is_pending_false_when_expired() -> None:
    """Construct an Invitation directly with a past expires_at."""
    from datetime import datetime as _dt

    past = datetime.now(tz=timezone.utc) - timedelta(hours=1)
    inv = Invitation(
        id=1,
        organization_id=1,
        email="a@b.c",
        role="staff",
        token="x" * 40,
        expires_at=past,
        created_at=_dt.now(tz=timezone.utc),
    )
    assert inv.is_pending() is False
