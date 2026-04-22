"""Tests for organizations, memberships, and Scope primitive (Phase 0.B)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from docstats.domain.orgs import ROLES, Membership, Organization, has_role_at_least
from docstats.routes._common import get_scope
from docstats.scope import Scope
from docstats.storage import Storage


@pytest.fixture
def user_id(storage: Storage) -> int:
    return storage.create_user("owner@example.com", "hashed")


@pytest.fixture
def other_user_id(storage: Storage) -> int:
    return storage.create_user("member@example.com", "hashed")


# --- Role hierarchy ---


def test_roles_are_ordered_low_to_high() -> None:
    assert ROLES == ("read_only", "staff", "clinician", "coordinator", "admin", "owner")


def test_has_role_at_least_true_at_and_above() -> None:
    assert has_role_at_least("owner", "admin") is True
    assert has_role_at_least("owner", "owner") is True
    assert has_role_at_least("admin", "coordinator") is True


def test_has_role_at_least_false_below() -> None:
    assert has_role_at_least("staff", "admin") is False
    assert has_role_at_least("read_only", "staff") is False


def test_has_role_at_least_unknown_role_returns_false() -> None:
    assert has_role_at_least("not_a_role", "admin") is False
    assert has_role_at_least(None, "admin") is False


def test_has_role_at_least_unknown_required_raises() -> None:
    with pytest.raises(ValueError):
        has_role_at_least("owner", "superuser")


# --- Scope dataclass ---


def test_scope_anonymous_by_default() -> None:
    scope = Scope()
    assert scope.is_anonymous is True
    assert scope.is_solo is False
    assert scope.is_org is False


def test_scope_solo_with_user_id() -> None:
    scope = Scope(user_id=42)
    assert scope.is_anonymous is False
    assert scope.is_solo is True
    assert scope.is_org is False


def test_scope_org_with_org_id() -> None:
    scope = Scope(user_id=42, organization_id=7, membership_role="admin")
    assert scope.is_anonymous is False
    assert scope.is_solo is False
    assert scope.is_org is True
    assert scope.membership_role == "admin"


def test_scope_rejects_role_without_org() -> None:
    with pytest.raises(ValueError, match="organization_id"):
        Scope(user_id=42, membership_role="admin")


def test_scope_is_frozen() -> None:
    scope = Scope(user_id=1)
    with pytest.raises(Exception):
        scope.user_id = 2  # type: ignore[misc]


# --- Organizations storage ---


def test_create_and_fetch_organization(storage: Storage) -> None:
    org = storage.create_organization(
        name="Robs Clinic",
        slug="robs-clinic",
        npi="1111111111",
        address_line1="1 Main St",
        address_city="SF",
        address_state="CA",
        address_zip="94110",
        phone="4155550001",
        fax="4155550002",
        terms_bundle_version="1.0",
    )
    assert org.id > 0
    assert isinstance(org, Organization)
    assert org.slug == "robs-clinic"
    assert org.stale_threshold_days == 3
    assert org.deleted_at is None

    fetched = storage.get_organization(org.id)
    assert fetched is not None
    assert fetched.name == "Robs Clinic"
    assert fetched.phone == "4155550001"

    by_slug = storage.get_organization_by_slug("robs-clinic")
    assert by_slug is not None
    assert by_slug.id == org.id


def test_create_organization_accepts_stale_threshold(storage: Storage) -> None:
    org = storage.create_organization(name="Timed Clinic", slug="timed", stale_threshold_days=7)
    assert org.stale_threshold_days == 7
    assert storage.get_organization(org.id).stale_threshold_days == 7  # type: ignore[union-attr]


def test_create_organization_rejects_invalid_stale_threshold(storage: Storage) -> None:
    with pytest.raises(ValueError, match="stale_threshold_days"):
        storage.create_organization(name="Too Fast", slug="fast", stale_threshold_days=0)
    with pytest.raises(ValueError, match="stale_threshold_days"):
        storage.create_organization(name="Too Slow", slug="slow", stale_threshold_days=366)


def test_soft_delete_hides_organization(storage: Storage) -> None:
    org = storage.create_organization(name="Gone Clinic", slug="gone")
    assert storage.soft_delete_organization(org.id) is True
    assert storage.get_organization(org.id) is None
    assert storage.get_organization_by_slug("gone") is None
    # Second delete is a no-op.
    assert storage.soft_delete_organization(org.id) is False


def test_slug_can_be_reused_after_soft_delete(storage: Storage) -> None:
    first = storage.create_organization(name="Clinic A", slug="acme")
    storage.soft_delete_organization(first.id)
    # Should succeed — partial unique index only applies to live rows.
    second = storage.create_organization(name="Clinic B", slug="acme")
    assert second.id != first.id


# --- update_organization (Phase 6.D) ---


def test_update_organization_leaves_unchanged_by_default(storage: Storage) -> None:
    """``None`` kwargs with default ``overwrite=False`` mean "leave unchanged"."""
    org = storage.create_organization(
        name="Original",
        slug="orig",
        npi="1111111111",
        address_city="SF",
        phone="4155550001",
    )
    # Caller passes only a new name; everything else is untouched.
    updated = storage.update_organization(org.id, name="Renamed")
    assert updated is not None
    assert updated.name == "Renamed"
    assert updated.npi == "1111111111"  # preserved
    assert updated.address_city == "SF"
    assert updated.phone == "4155550001"
    assert updated.stale_threshold_days == 3


def test_update_organization_stale_threshold(storage: Storage) -> None:
    org = storage.create_organization(name="Notify", slug="notify")
    updated = storage.update_organization(org.id, stale_threshold_days=10)
    assert updated is not None
    assert updated.stale_threshold_days == 10


def test_update_organization_rejects_invalid_stale_threshold(storage: Storage) -> None:
    org = storage.create_organization(name="Notify", slug="notify-invalid")
    with pytest.raises(ValueError, match="threshold"):
        storage.update_organization(org.id, stale_threshold_days=0)
    with pytest.raises(ValueError, match="threshold"):
        storage.update_organization(org.id, stale_threshold_days=366)


def test_update_organization_overwrite_clears_nulls(storage: Storage) -> None:
    """With ``overwrite=True``, ``None`` kwargs write ``NULL`` to the column."""
    org = storage.create_organization(
        name="Original",
        slug="overwrite",
        npi="1111111111",
        address_line1="100 Old St",
        phone="4155550001",
        fax="4155550002",
    )
    updated = storage.update_organization(
        org.id,
        name="Original",  # schema requires non-empty; pass existing value
        npi=None,
        address_line1=None,
        phone=None,
        fax=None,
        overwrite=True,
    )
    assert updated is not None
    assert updated.name == "Original"
    assert updated.npi is None
    assert updated.address_line1 is None
    assert updated.phone is None
    assert updated.fax is None


def test_update_organization_overwrite_rejects_empty_name(storage: Storage) -> None:
    """``organizations.name`` is NOT NULL in the schema; overwrite mode must
    fail fast rather than let the DB reject the write with a cryptic error."""
    org = storage.create_organization(name="Keep", slug="keep")
    with pytest.raises(ValueError, match="non-empty"):
        storage.update_organization(org.id, name=None, overwrite=True)
    with pytest.raises(ValueError, match="non-empty"):
        storage.update_organization(org.id, name="   ", overwrite=True)


def test_update_organization_returns_none_for_missing(storage: Storage) -> None:
    assert storage.update_organization(9999, name="Whatever") is None


def test_update_organization_returns_none_for_soft_deleted(storage: Storage) -> None:
    org = storage.create_organization(name="Ghost", slug="ghost")
    storage.soft_delete_organization(org.id)
    assert storage.update_organization(org.id, name="Zombie") is None


def test_update_organization_does_not_touch_slug(storage: Storage) -> None:
    """``slug`` is intentionally not an accepted kwarg — changing it would
    break bookmarked URLs. The method signature enforces this."""
    org = storage.create_organization(name="Stable", slug="stable-slug")
    updated = storage.update_organization(org.id, name="Renamed")
    assert updated is not None
    assert updated.slug == "stable-slug"


def test_update_organization_no_kwargs_returns_current_row(storage: Storage) -> None:
    """Calling with no fields is a valid no-op; return the current row."""
    org = storage.create_organization(name="NoOp", slug="noop")
    unchanged = storage.update_organization(org.id)
    assert unchanged is not None
    assert unchanged.id == org.id
    assert unchanged.name == "NoOp"


# --- Memberships storage ---


def test_create_and_fetch_membership(storage: Storage, user_id: int) -> None:
    org = storage.create_organization(name="C", slug="c")
    m = storage.create_membership(organization_id=org.id, user_id=user_id, role="owner")
    assert isinstance(m, Membership)
    assert m.role == "owner"
    assert m.is_active is True

    fetched = storage.get_membership(org.id, user_id)
    assert fetched is not None
    assert fetched.id == m.id


def test_create_membership_rejects_unknown_role(storage: Storage, user_id: int) -> None:
    org = storage.create_organization(name="C", slug="c")
    with pytest.raises(ValueError, match="Unknown role"):
        storage.create_membership(organization_id=org.id, user_id=user_id, role="god")


def test_list_memberships_for_user(storage: Storage, user_id: int) -> None:
    org1 = storage.create_organization(name="One", slug="one")
    org2 = storage.create_organization(name="Two", slug="two")
    storage.create_membership(organization_id=org1.id, user_id=user_id, role="owner")
    storage.create_membership(organization_id=org2.id, user_id=user_id, role="coordinator")

    memberships = storage.list_memberships_for_user(user_id)
    assert len(memberships) == 2
    # joined_at DESC, id DESC — most recent first.
    assert memberships[0].organization_id == org2.id


def test_list_memberships_for_org(storage: Storage, user_id: int, other_user_id: int) -> None:
    org = storage.create_organization(name="C", slug="c")
    storage.create_membership(organization_id=org.id, user_id=user_id, role="owner")
    storage.create_membership(organization_id=org.id, user_id=other_user_id, role="staff")

    members = storage.list_memberships_for_org(org.id)
    assert len(members) == 2
    # joined_at ASC, id ASC — oldest (owner) first.
    assert members[0].user_id == user_id
    assert members[0].role == "owner"


def test_update_membership_role(storage: Storage, user_id: int) -> None:
    org = storage.create_organization(name="C", slug="c")
    storage.create_membership(organization_id=org.id, user_id=user_id, role="staff")
    assert storage.update_membership_role(org.id, user_id, "coordinator") is True
    assert storage.get_membership(org.id, user_id).role == "coordinator"  # type: ignore[union-attr]


def test_update_membership_role_rejects_unknown(storage: Storage, user_id: int) -> None:
    org = storage.create_organization(name="C", slug="c")
    storage.create_membership(organization_id=org.id, user_id=user_id, role="staff")
    with pytest.raises(ValueError, match="Unknown role"):
        storage.update_membership_role(org.id, user_id, "god")


def test_soft_delete_membership(storage: Storage, user_id: int) -> None:
    org = storage.create_organization(name="C", slug="c")
    storage.create_membership(organization_id=org.id, user_id=user_id, role="owner")
    assert storage.soft_delete_membership(org.id, user_id) is True
    assert storage.get_membership(org.id, user_id) is None
    assert storage.list_memberships_for_user(user_id) == []
    # Second delete is a no-op.
    assert storage.soft_delete_membership(org.id, user_id) is False


def test_rejoin_after_soft_delete_reactivates(storage: Storage, user_id: int) -> None:
    """Re-inviting a soft-deleted member reactivates the existing row.

    The UNIQUE constraint on (organization_id, user_id) is unconditional, so
    a second INSERT would fail. The upsert path clears ``deleted_at``,
    refreshes ``joined_at``, and updates ``role`` / ``invited_by_user_id``
    from the new invite.
    """
    org = storage.create_organization(name="C", slug="c")
    first = storage.create_membership(organization_id=org.id, user_id=user_id, role="staff")
    storage.soft_delete_membership(org.id, user_id)

    # Re-invite with a different role. Should reactivate the existing row.
    second = storage.create_membership(
        organization_id=org.id, user_id=user_id, role="admin", invited_by_user_id=user_id
    )
    assert second.id == first.id  # same row, reactivated
    assert second.role == "admin"  # new role overwrote
    assert second.invited_by_user_id == user_id
    assert second.deleted_at is None
    # And the member shows up in active lists again.
    assert storage.get_membership(org.id, user_id) is not None
    assert len(storage.list_memberships_for_user(user_id)) == 1


# --- active_org_id ---


def test_set_active_org_persists(storage: Storage, user_id: int) -> None:
    org = storage.create_organization(name="C", slug="c")
    storage.set_active_org(user_id, org.id)

    user = storage.get_user_by_id(user_id)
    assert user is not None
    assert user["active_org_id"] == org.id

    storage.set_active_org(user_id, None)
    user = storage.get_user_by_id(user_id)
    assert user is not None
    assert user["active_org_id"] is None


# --- get_scope dependency ---


def test_get_scope_anonymous() -> None:
    scope = get_scope(current_user=None, storage=MagicMock())
    assert scope.is_anonymous is True


def test_get_scope_solo_when_no_active_org(storage: Storage, user_id: int) -> None:
    user = storage.get_user_by_id(user_id)
    scope = get_scope(current_user=user, storage=storage)
    assert scope.is_solo is True
    assert scope.user_id == user_id
    assert scope.organization_id is None


def test_get_scope_org_when_active_membership(storage: Storage, user_id: int) -> None:
    org = storage.create_organization(name="C", slug="c")
    storage.create_membership(organization_id=org.id, user_id=user_id, role="admin")
    storage.set_active_org(user_id, org.id)

    user = storage.get_user_by_id(user_id)
    scope = get_scope(current_user=user, storage=storage)
    assert scope.is_org is True
    assert scope.user_id == user_id
    assert scope.organization_id == org.id
    assert scope.membership_role == "admin"


def test_get_scope_falls_back_to_solo_on_stale_active_org(storage: Storage, user_id: int) -> None:
    """If active_org_id points at an org the user isn't a member of anymore
    (soft-deleted membership, removed from org, etc.), get_scope silently
    falls back to solo and clears the stale pointer."""
    org = storage.create_organization(name="C", slug="c")
    storage.create_membership(organization_id=org.id, user_id=user_id, role="owner")
    storage.set_active_org(user_id, org.id)
    storage.soft_delete_membership(org.id, user_id)

    user = storage.get_user_by_id(user_id)
    assert user is not None
    assert user["active_org_id"] == org.id  # stale pointer
    scope = get_scope(current_user=user, storage=storage)
    assert scope.is_solo is True
    assert scope.user_id == user_id

    # Stale pointer was cleared.
    refreshed = storage.get_user_by_id(user_id)
    assert refreshed is not None
    assert refreshed["active_org_id"] is None


def test_get_scope_tolerates_set_active_org_failure(storage: Storage, user_id: int) -> None:
    """Stale-pointer clearing is best-effort; if set_active_org raises, scope
    resolution still succeeds with solo-mode fallback."""
    # Build a half-mock storage that delegates reads to real storage but
    # raises on set_active_org.
    org = storage.create_organization(name="C", slug="c")
    storage.create_membership(organization_id=org.id, user_id=user_id, role="owner")
    storage.set_active_org(user_id, org.id)
    storage.soft_delete_membership(org.id, user_id)
    user = storage.get_user_by_id(user_id)

    wrapper = MagicMock(wraps=storage)
    wrapper.set_active_org.side_effect = RuntimeError("db offline")

    scope = get_scope(current_user=user, storage=wrapper)
    assert scope.is_solo is True
