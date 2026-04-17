"""Tests for patients — scope-enforced first-class entity (Phase 1.A)."""

from __future__ import annotations

import sqlite3

import pytest

from docstats.domain.patients import Patient
from docstats.scope import Scope, ScopeRequired, scope_sql_clause
from docstats.storage import Storage


@pytest.fixture
def user_a(storage: Storage) -> int:
    return storage.create_user("a@example.com", "hashed")


@pytest.fixture
def user_b(storage: Storage) -> int:
    return storage.create_user("b@example.com", "hashed")


@pytest.fixture
def org_a(storage: Storage, user_a: int) -> int:
    org = storage.create_organization(name="Org A", slug="org-a")
    storage.create_membership(organization_id=org.id, user_id=user_a, role="owner")
    return org.id


@pytest.fixture
def org_b(storage: Storage, user_b: int) -> int:
    org = storage.create_organization(name="Org B", slug="org-b")
    storage.create_membership(organization_id=org.id, user_id=user_b, role="owner")
    return org.id


# --- scope_sql_clause helper ---


def test_scope_sql_clause_solo() -> None:
    clause, params = scope_sql_clause(Scope(user_id=42))
    assert clause == "scope_user_id = ? AND scope_organization_id IS NULL"
    assert params == [42]


def test_scope_sql_clause_org() -> None:
    clause, params = scope_sql_clause(Scope(user_id=42, organization_id=7, membership_role="admin"))
    assert clause == "scope_organization_id = ? AND scope_user_id IS NULL"
    assert params == [7]


def test_scope_sql_clause_anonymous_raises() -> None:
    with pytest.raises(ScopeRequired):
        scope_sql_clause(Scope())


def test_scope_sql_clause_accepts_custom_cols() -> None:
    clause, _ = scope_sql_clause(Scope(user_id=42), user_col="owner_uid", org_col="owner_oid")
    assert clause == "owner_uid = ? AND owner_oid IS NULL"


# --- create_patient ---


def test_create_patient_solo_mode(storage: Storage, user_a: int) -> None:
    scope = Scope(user_id=user_a)
    p = storage.create_patient(
        scope,
        first_name="Jane",
        last_name="Doe",
        date_of_birth="1980-05-15",
        created_by_user_id=user_a,
    )
    assert isinstance(p, Patient)
    assert p.id > 0
    assert p.scope_user_id == user_a
    assert p.scope_organization_id is None
    assert p.first_name == "Jane"
    assert p.display_name == "Jane Doe"
    assert p.deleted_at is None


def test_create_patient_org_mode(storage: Storage, user_a: int, org_a: int) -> None:
    scope = Scope(user_id=user_a, organization_id=org_a, membership_role="owner")
    p = storage.create_patient(
        scope,
        first_name="John",
        last_name="Smith",
        middle_name="Q",
        mrn="MRN-001",
        created_by_user_id=user_a,
    )
    assert p.scope_organization_id == org_a
    assert p.scope_user_id is None
    assert p.mrn == "MRN-001"
    assert p.display_name == "John Q Smith"


def test_create_patient_rejects_anonymous_scope(storage: Storage) -> None:
    with pytest.raises(ScopeRequired):
        storage.create_patient(Scope(), first_name="No", last_name="Scope")


def test_create_patient_check_constraint_enforced(storage: Storage, user_a: int) -> None:
    """Both scope cols NULL or both set must be rejected at DB level.

    Can't hit this through the normal create_patient path (Scope guarantees
    XOR). Verify via raw SQL so a future code change that bypasses Scope
    still gets caught by the DB.
    """
    with pytest.raises(sqlite3.IntegrityError):
        storage._conn.execute(
            "INSERT INTO patients (scope_user_id, scope_organization_id, first_name, last_name) "
            "VALUES (?, ?, ?, ?)",
            (None, None, "Both", "Null"),
        )
        storage._conn.commit()
    storage._conn.rollback()


# --- get_patient + cross-tenant isolation ---


def test_get_patient_own_scope_returns_row(storage: Storage, user_a: int) -> None:
    scope = Scope(user_id=user_a)
    created = storage.create_patient(scope, first_name="Jane", last_name="Doe")
    fetched = storage.get_patient(scope, created.id)
    assert fetched is not None
    assert fetched.id == created.id


def test_get_patient_other_user_scope_returns_none(
    storage: Storage, user_a: int, user_b: int
) -> None:
    """Cross-tenant isolation: user B cannot read user A's patient."""
    scope_a = Scope(user_id=user_a)
    scope_b = Scope(user_id=user_b)
    p_a = storage.create_patient(scope_a, first_name="A's", last_name="Patient")

    assert storage.get_patient(scope_b, p_a.id) is None


def test_get_patient_other_org_scope_returns_none(
    storage: Storage, user_a: int, org_a: int, org_b: int
) -> None:
    """Cross-tenant isolation: org B cannot read org A's patient."""
    scope_a = Scope(user_id=user_a, organization_id=org_a, membership_role="owner")
    # Use a dummy membership-free scope for B to avoid coupling to user_b's
    # membership fixture order; manually build the Scope since scope_sql_clause
    # only needs organization_id + membership_role sentinel.
    scope_b = Scope(user_id=999, organization_id=org_b, membership_role="owner")
    p_a = storage.create_patient(scope_a, first_name="A's", last_name="Patient")

    assert storage.get_patient(scope_b, p_a.id) is None


def test_get_patient_solo_cannot_read_org_patient(
    storage: Storage, user_a: int, org_a: int
) -> None:
    """A user acting in solo mode cannot read a patient they created in org
    mode. Scope mode is part of the access key, not just the user id."""
    org_scope = Scope(user_id=user_a, organization_id=org_a, membership_role="owner")
    solo_scope = Scope(user_id=user_a)
    p = storage.create_patient(org_scope, first_name="Org", last_name="Pt")

    assert storage.get_patient(solo_scope, p.id) is None


def test_get_patient_hides_soft_deleted(storage: Storage, user_a: int) -> None:
    scope = Scope(user_id=user_a)
    p = storage.create_patient(scope, first_name="Deleted", last_name="Pt")
    storage.soft_delete_patient(scope, p.id)
    assert storage.get_patient(scope, p.id) is None


# --- list_patients ---


def test_list_patients_scope_filtered(storage: Storage, user_a: int, user_b: int) -> None:
    storage.create_patient(Scope(user_id=user_a), first_name="A1", last_name="Pt")
    storage.create_patient(Scope(user_id=user_a), first_name="A2", last_name="Pt")
    storage.create_patient(Scope(user_id=user_b), first_name="B1", last_name="Pt")

    a_patients = storage.list_patients(Scope(user_id=user_a))
    b_patients = storage.list_patients(Scope(user_id=user_b))

    assert {p.first_name for p in a_patients} == {"A1", "A2"}
    assert {p.first_name for p in b_patients} == {"B1"}


def test_list_patients_orders_by_name(storage: Storage, user_a: int) -> None:
    scope = Scope(user_id=user_a)
    storage.create_patient(scope, first_name="Alice", last_name="Zulu")
    storage.create_patient(scope, first_name="Bob", last_name="Alpha")
    storage.create_patient(scope, first_name="Charlie", last_name="Alpha")

    names = [p.first_name for p in storage.list_patients(scope)]
    # last_name ASC, then first_name ASC.
    assert names == ["Bob", "Charlie", "Alice"]


def test_list_patients_excludes_deleted_by_default(storage: Storage, user_a: int) -> None:
    scope = Scope(user_id=user_a)
    p = storage.create_patient(scope, first_name="Doomed", last_name="Pt")
    storage.soft_delete_patient(scope, p.id)
    assert storage.list_patients(scope) == []
    # But include_deleted=True surfaces them for admin audit flows.
    assert len(storage.list_patients(scope, include_deleted=True)) == 1


def test_list_patients_search_matches_name_and_mrn(
    storage: Storage, user_a: int, org_a: int
) -> None:
    scope = Scope(user_id=user_a, organization_id=org_a, membership_role="owner")
    storage.create_patient(scope, first_name="Jane", last_name="Doe", mrn="MRN-1")
    storage.create_patient(scope, first_name="John", last_name="Smith", mrn="MRN-2")
    storage.create_patient(scope, first_name="Alice", last_name="Johnson", mrn="X-3")

    assert len(storage.list_patients(scope, search="john")) == 2  # John Smith + Johnson
    assert len(storage.list_patients(scope, search="MRN")) == 2
    assert len(storage.list_patients(scope, search="X-3")) == 1
    assert len(storage.list_patients(scope, search="nobody")) == 0


def test_list_patients_limit_and_offset(storage: Storage, user_a: int) -> None:
    scope = Scope(user_id=user_a)
    for i in range(5):
        storage.create_patient(scope, first_name=f"P{i}", last_name="X")
    assert len(storage.list_patients(scope, limit=2)) == 2
    assert len(storage.list_patients(scope, limit=2, offset=2)) == 2
    assert len(storage.list_patients(scope, limit=2, offset=4)) == 1


def test_list_patients_search_escapes_like_wildcards(storage: Storage, user_a: int) -> None:
    """A user-supplied ``%`` in search must not match everything."""
    scope = Scope(user_id=user_a)
    storage.create_patient(scope, first_name="Real", last_name="Match")
    storage.create_patient(scope, first_name="Other", last_name="Row")
    assert storage.list_patients(scope, search="%") == []


def test_list_patients_anonymous_raises(storage: Storage) -> None:
    with pytest.raises(ScopeRequired):
        storage.list_patients(Scope())


# --- update_patient ---


def test_update_patient_changes_fields(storage: Storage, user_a: int) -> None:
    scope = Scope(user_id=user_a)
    p = storage.create_patient(scope, first_name="Jane", last_name="Doe")
    updated = storage.update_patient(scope, p.id, first_name="Janet", phone="4155551234")
    assert updated is not None
    assert updated.first_name == "Janet"
    assert updated.last_name == "Doe"  # untouched
    assert updated.phone == "4155551234"
    assert updated.updated_at >= p.updated_at


def test_update_patient_cross_tenant_returns_none(
    storage: Storage, user_a: int, user_b: int
) -> None:
    """User B attempting to update user A's patient gets None — and user A's
    row is untouched."""
    scope_a = Scope(user_id=user_a)
    scope_b = Scope(user_id=user_b)
    p = storage.create_patient(scope_a, first_name="Jane", last_name="Doe")

    result = storage.update_patient(scope_b, p.id, first_name="Hijacked")
    assert result is None

    refetched = storage.get_patient(scope_a, p.id)
    assert refetched is not None
    assert refetched.first_name == "Jane"  # unchanged


def test_update_patient_no_fields_is_noop(storage: Storage, user_a: int) -> None:
    scope = Scope(user_id=user_a)
    p = storage.create_patient(scope, first_name="Jane", last_name="Doe")
    updated = storage.update_patient(scope, p.id)
    assert updated is not None
    assert updated.id == p.id


def test_update_patient_ignores_deleted(storage: Storage, user_a: int) -> None:
    scope = Scope(user_id=user_a)
    p = storage.create_patient(scope, first_name="Jane", last_name="Doe")
    storage.soft_delete_patient(scope, p.id)
    assert storage.update_patient(scope, p.id, first_name="Ghost") is None


# --- soft_delete_patient ---


def test_soft_delete_patient(storage: Storage, user_a: int) -> None:
    scope = Scope(user_id=user_a)
    p = storage.create_patient(scope, first_name="Doomed", last_name="Pt")
    assert storage.soft_delete_patient(scope, p.id) is True
    assert storage.get_patient(scope, p.id) is None
    # Double-delete is a no-op.
    assert storage.soft_delete_patient(scope, p.id) is False


def test_soft_delete_patient_cross_tenant_returns_false(
    storage: Storage, user_a: int, user_b: int
) -> None:
    scope_a = Scope(user_id=user_a)
    scope_b = Scope(user_id=user_b)
    p = storage.create_patient(scope_a, first_name="Jane", last_name="Doe")
    assert storage.soft_delete_patient(scope_b, p.id) is False
    assert storage.get_patient(scope_a, p.id) is not None


# --- MRN uniqueness ---


def test_mrn_unique_within_org(storage: Storage, user_a: int, org_a: int) -> None:
    scope = Scope(user_id=user_a, organization_id=org_a, membership_role="owner")
    storage.create_patient(scope, first_name="A", last_name="X", mrn="MRN-1")
    with pytest.raises(sqlite3.IntegrityError):
        storage.create_patient(scope, first_name="B", last_name="Y", mrn="MRN-1")


def test_mrn_reusable_after_soft_delete(storage: Storage, user_a: int, org_a: int) -> None:
    """The MRN unique index is partial (WHERE deleted_at IS NULL), so a
    soft-deleted patient frees the MRN for reuse on a re-admit."""
    scope = Scope(user_id=user_a, organization_id=org_a, membership_role="owner")
    first = storage.create_patient(scope, first_name="A", last_name="X", mrn="MRN-1")
    storage.soft_delete_patient(scope, first.id)
    # Reuse should succeed.
    second = storage.create_patient(scope, first_name="B", last_name="Y", mrn="MRN-1")
    assert second.id != first.id


def test_mrn_unique_across_orgs_is_fine(
    storage: Storage, user_a: int, org_a: int, org_b: int
) -> None:
    """MRN is org-scoped; the same MRN can exist in two different orgs."""
    scope_a = Scope(user_id=user_a, organization_id=org_a, membership_role="owner")
    scope_b = Scope(user_id=999, organization_id=org_b, membership_role="owner")
    storage.create_patient(scope_a, first_name="A", last_name="X", mrn="SHARED-MRN")
    storage.create_patient(scope_b, first_name="B", last_name="Y", mrn="SHARED-MRN")
    # No constraint violation.


# --- Cascade on org deletion ---


def test_org_delete_cascades_patients(storage: Storage, user_a: int, org_a: int) -> None:
    """Deleting an organization cascades to its patients (CASCADE FK)."""
    scope = Scope(user_id=user_a, organization_id=org_a, membership_role="owner")
    p = storage.create_patient(scope, first_name="A", last_name="X")
    storage._conn.execute("DELETE FROM organizations WHERE id = ?", (org_a,))
    storage._conn.commit()
    # Patient row is gone entirely (not just soft-deleted).
    row = storage._conn.execute("SELECT * FROM patients WHERE id = ?", (p.id,)).fetchone()
    assert row is None
