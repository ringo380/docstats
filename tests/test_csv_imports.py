"""Tests for CSV bulk import staging tables (Phase 1.F)."""

from __future__ import annotations

import sqlite3

import pytest

from docstats.domain.imports import (
    IMPORT_ROW_STATUS_VALUES,
    IMPORT_STATUS_TRANSITIONS,
    IMPORT_STATUS_VALUES,
    IMPORT_TERMINAL_STATUSES,
    InvalidImportRowTransition,
    InvalidImportTransition,
    CsvImport,
    CsvImportRow,
    import_transition_allowed,
    require_import_transition,
    require_row_transition,
    row_transition_allowed,
)
from docstats.scope import Scope, ScopeRequired
from docstats.storage import Storage


# --- Fixtures ---


@pytest.fixture
def user_a(storage: Storage) -> int:
    return storage.create_user("a@csv.com", "hashed")


@pytest.fixture
def user_b(storage: Storage) -> int:
    return storage.create_user("b@csv.com", "hashed")


@pytest.fixture
def scope_a(user_a: int) -> Scope:
    return Scope(user_id=user_a)


@pytest.fixture
def scope_b(user_b: int) -> Scope:
    return Scope(user_id=user_b)


@pytest.fixture
def import_a(storage: Storage, scope_a: Scope, user_a: int) -> int:
    """Create a csv_import for user A and return its id."""
    return storage.create_csv_import(
        scope_a,
        original_filename="referrals-2026-04.csv",
        uploaded_by_user_id=user_a,
        row_count=10,
    ).id


# ============================================================
# State machine
# ============================================================


def test_all_import_status_values_have_transitions() -> None:
    for s in IMPORT_STATUS_VALUES:
        assert s in IMPORT_STATUS_TRANSITIONS


def test_import_terminal_statuses_no_outgoing() -> None:
    for s in IMPORT_TERMINAL_STATUSES:
        assert IMPORT_STATUS_TRANSITIONS[s] == frozenset()


def test_import_transition_happy_path() -> None:
    assert import_transition_allowed("uploaded", "mapped")
    assert import_transition_allowed("mapped", "validated")
    assert import_transition_allowed("validated", "committed")
    assert import_transition_allowed("uploaded", "failed")


def test_import_transition_rejects_illegal() -> None:
    assert not import_transition_allowed("uploaded", "committed")  # skip stages
    assert not import_transition_allowed("committed", "uploaded")  # terminal
    assert not import_transition_allowed("failed", "mapped")  # terminal


def test_import_transition_rejects_unknown_from() -> None:
    assert not import_transition_allowed("mystery", "mapped")


def test_require_import_transition_raises() -> None:
    with pytest.raises(InvalidImportTransition):
        require_import_transition("uploaded", "committed")


def test_row_transition_happy_path() -> None:
    assert row_transition_allowed("pending", "valid")
    assert row_transition_allowed("valid", "committed")
    assert row_transition_allowed("error", "valid")
    assert row_transition_allowed("valid", "error")


def test_row_transition_rejects_illegal() -> None:
    assert not row_transition_allowed("pending", "committed")
    assert not row_transition_allowed("committed", "valid")  # terminal
    assert not row_transition_allowed("skipped", "valid")  # terminal


def test_require_row_transition_raises() -> None:
    with pytest.raises(InvalidImportRowTransition):
        require_row_transition("committed", "valid")


# ============================================================
# csv_imports (scope-owned)
# ============================================================


def test_create_csv_import_solo(storage: Storage, scope_a: Scope, user_a: int) -> None:
    imp = storage.create_csv_import(
        scope_a,
        original_filename="foo.csv",
        uploaded_by_user_id=user_a,
        row_count=5,
    )
    assert isinstance(imp, CsvImport)
    assert imp.scope_user_id == scope_a.user_id
    assert imp.scope_organization_id is None
    assert imp.status == "uploaded"
    assert imp.row_count == 5
    assert imp.mapping == {}
    assert imp.error_report == {}


def test_create_csv_import_with_mapping(storage: Storage, scope_a: Scope) -> None:
    imp = storage.create_csv_import(
        scope_a,
        original_filename="referrals.csv",
        mapping={"Patient First": "patient_first_name", "NPI": "receiving_npi"},
    )
    assert imp.mapping == {
        "Patient First": "patient_first_name",
        "NPI": "receiving_npi",
    }


def test_create_csv_import_rejects_anonymous(storage: Storage) -> None:
    with pytest.raises(ScopeRequired):
        storage.create_csv_import(Scope(), original_filename="nope.csv")


def test_get_csv_import_cross_tenant_returns_none(
    storage: Storage, scope_a: Scope, scope_b: Scope, import_a: int
) -> None:
    assert storage.get_csv_import(scope_b, import_a) is None
    assert storage.get_csv_import(scope_a, import_a) is not None


def test_list_csv_imports_scope_filtered(storage: Storage, scope_a: Scope, scope_b: Scope) -> None:
    storage.create_csv_import(scope_a, original_filename="a1.csv")
    storage.create_csv_import(scope_a, original_filename="a2.csv")
    storage.create_csv_import(scope_b, original_filename="b1.csv")

    a_list = storage.list_csv_imports(scope_a)
    b_list = storage.list_csv_imports(scope_b)
    assert {i.original_filename for i in a_list} == {"a1.csv", "a2.csv"}
    assert {i.original_filename for i in b_list} == {"b1.csv"}


def test_update_csv_import_status(storage: Storage, scope_a: Scope, import_a: int) -> None:
    updated = storage.update_csv_import(scope_a, import_a, status="mapped")
    assert updated is not None
    assert updated.status == "mapped"


def test_update_csv_import_rejects_unknown_status(
    storage: Storage, scope_a: Scope, import_a: int
) -> None:
    with pytest.raises(ValueError, match="status"):
        storage.update_csv_import(scope_a, import_a, status="bogus")


def test_all_import_statuses_writable(storage: Storage, scope_a: Scope, import_a: int) -> None:
    for s in IMPORT_STATUS_VALUES:
        updated = storage.update_csv_import(scope_a, import_a, status=s)
        assert updated is not None
        assert updated.status == s


def test_update_csv_import_cross_tenant_returns_none(
    storage: Storage, scope_b: Scope, import_a: int
) -> None:
    assert storage.update_csv_import(scope_b, import_a, status="mapped") is None


def test_update_csv_import_preserves_other_fields(
    storage: Storage, scope_a: Scope, import_a: int
) -> None:
    storage.update_csv_import(scope_a, import_a, mapping={"col": "field"})
    storage.update_csv_import(scope_a, import_a, status="mapped")
    fresh = storage.get_csv_import(scope_a, import_a)
    assert fresh is not None
    assert fresh.mapping == {"col": "field"}
    assert fresh.status == "mapped"


def test_update_csv_import_error_report(storage: Storage, scope_a: Scope, import_a: int) -> None:
    report = {"total_errors": 3, "by_field": {"npi": 2, "dob": 1}}
    updated = storage.update_csv_import(scope_a, import_a, error_report=report)
    assert updated is not None
    assert updated.error_report == report


def test_delete_csv_import_hard_delete(storage: Storage, scope_a: Scope, import_a: int) -> None:
    assert storage.delete_csv_import(scope_a, import_a) is True
    assert storage.get_csv_import(scope_a, import_a) is None
    assert storage.delete_csv_import(scope_a, import_a) is False  # no-op


def test_delete_csv_import_cross_tenant_returns_false(
    storage: Storage, scope_b: Scope, import_a: int
) -> None:
    assert storage.delete_csv_import(scope_b, import_a) is False


def test_csv_import_check_constraint_on_scope(storage: Storage) -> None:
    """Both scope cols NULL must be rejected at DB level."""
    with pytest.raises(sqlite3.IntegrityError):
        storage._conn.execute(
            "INSERT INTO csv_imports (scope_user_id, scope_organization_id, original_filename) "
            "VALUES (NULL, NULL, 'orphan.csv')"
        )
        storage._conn.commit()
    storage._conn.rollback()


# ============================================================
# csv_import_rows (scope-transitive)
# ============================================================


def test_add_import_row(storage: Storage, scope_a: Scope, import_a: int) -> None:
    r = storage.add_csv_import_row(
        scope_a,
        import_a,
        row_index=1,
        raw_json={"Patient First": "Jane", "NPI": "1234567890"},
    )
    assert isinstance(r, CsvImportRow)
    assert r.import_id == import_a
    assert r.row_index == 1
    assert r.raw_json == {"Patient First": "Jane", "NPI": "1234567890"}
    assert r.status == "pending"
    assert r.referral_id is None


def test_add_import_row_rejects_unknown_status(
    storage: Storage, scope_a: Scope, import_a: int
) -> None:
    with pytest.raises(ValueError, match="status"):
        storage.add_csv_import_row(scope_a, import_a, row_index=1, status="quantum_superposition")


def test_all_row_statuses_writable(storage: Storage, scope_a: Scope, import_a: int) -> None:
    """Every IMPORT_ROW_STATUS_VALUES entry passes the SQL CHECK."""
    for i, s in enumerate(IMPORT_ROW_STATUS_VALUES, start=1):
        r = storage.add_csv_import_row(scope_a, import_a, row_index=i, status=s)
        assert r is not None and r.status == s


def test_add_import_row_cross_tenant_returns_none(
    storage: Storage, scope_b: Scope, import_a: int
) -> None:
    assert storage.add_csv_import_row(scope_b, import_a, row_index=1) is None
    # No row was written.
    assert (
        storage._conn.execute(
            "SELECT count(*) AS n FROM csv_import_rows WHERE import_id = ?",
            (import_a,),
        ).fetchone()["n"]
        == 0
    )


def test_list_import_rows_cross_tenant_returns_empty(
    storage: Storage, scope_a: Scope, scope_b: Scope, import_a: int
) -> None:
    storage.add_csv_import_row(scope_a, import_a, row_index=1)
    assert storage.list_csv_import_rows(scope_b, import_a) == []


def test_list_import_rows_filter_by_status(storage: Storage, scope_a: Scope, import_a: int) -> None:
    storage.add_csv_import_row(scope_a, import_a, row_index=1, status="valid")
    storage.add_csv_import_row(scope_a, import_a, row_index=2, status="error")
    storage.add_csv_import_row(scope_a, import_a, row_index=3, status="valid")

    only_errors = storage.list_csv_import_rows(scope_a, import_a, status="error")
    assert len(only_errors) == 1
    assert only_errors[0].row_index == 2


def test_list_import_rows_orders_by_row_index(
    storage: Storage, scope_a: Scope, import_a: int
) -> None:
    storage.add_csv_import_row(scope_a, import_a, row_index=3)
    storage.add_csv_import_row(scope_a, import_a, row_index=1)
    storage.add_csv_import_row(scope_a, import_a, row_index=2)
    rows = storage.list_csv_import_rows(scope_a, import_a)
    assert [r.row_index for r in rows] == [1, 2, 3]


def test_row_index_unique_per_import(storage: Storage, scope_a: Scope, import_a: int) -> None:
    """Unique index guards against double-inserts during upload parsing."""
    storage.add_csv_import_row(scope_a, import_a, row_index=1)
    with pytest.raises(sqlite3.IntegrityError):
        storage.add_csv_import_row(scope_a, import_a, row_index=1)


def test_update_import_row_fields(storage: Storage, scope_a: Scope, import_a: int) -> None:
    r = storage.add_csv_import_row(scope_a, import_a, row_index=1)
    assert r is not None
    updated = storage.update_csv_import_row(
        scope_a,
        import_a,
        r.id,
        status="error",
        validation_errors={"npi": "must be 10 digits"},
    )
    assert updated is not None
    assert updated.status == "error"
    assert updated.validation_errors == {"npi": "must be 10 digits"}


def test_update_import_row_links_to_referral(
    storage: Storage, scope_a: Scope, user_a: int, import_a: int
) -> None:
    """After batch commit, rows get linked to the referral they produced."""
    patient = storage.create_patient(scope_a, first_name="Jane", last_name="Doe")
    referral = storage.create_referral(scope_a, patient_id=patient.id)
    r = storage.add_csv_import_row(scope_a, import_a, row_index=1, status="valid")
    assert r is not None

    updated = storage.update_csv_import_row(
        scope_a, import_a, r.id, status="committed", referral_id=referral.id
    )
    assert updated is not None
    assert updated.status == "committed"
    assert updated.referral_id == referral.id


def test_update_import_row_cross_tenant_returns_none(
    storage: Storage, scope_a: Scope, scope_b: Scope, import_a: int
) -> None:
    r = storage.add_csv_import_row(scope_a, import_a, row_index=1)
    assert r is not None
    assert storage.update_csv_import_row(scope_b, import_a, r.id, status="valid") is None


def test_delete_import_row(storage: Storage, scope_a: Scope, import_a: int) -> None:
    r = storage.add_csv_import_row(scope_a, import_a, row_index=1)
    assert r is not None
    assert storage.delete_csv_import_row(scope_a, import_a, r.id) is True
    assert storage.list_csv_import_rows(scope_a, import_a) == []
    assert storage.delete_csv_import_row(scope_a, import_a, r.id) is False


def test_delete_import_row_cross_tenant_returns_false(
    storage: Storage, scope_a: Scope, scope_b: Scope, import_a: int
) -> None:
    r = storage.add_csv_import_row(scope_a, import_a, row_index=1)
    assert r is not None
    assert storage.delete_csv_import_row(scope_b, import_a, r.id) is False


# ============================================================
# Cascade + cleanup
# ============================================================


def test_hard_delete_import_cascades_rows(storage: Storage, scope_a: Scope, import_a: int) -> None:
    storage.add_csv_import_row(scope_a, import_a, row_index=1)
    storage.add_csv_import_row(scope_a, import_a, row_index=2)
    storage.delete_csv_import(scope_a, import_a)
    # Rows cascade.
    assert (
        storage._conn.execute(
            "SELECT count(*) AS n FROM csv_import_rows WHERE import_id = ?",
            (import_a,),
        ).fetchone()["n"]
        == 0
    )


def test_referral_delete_nulls_import_row_fk(
    storage: Storage, scope_a: Scope, import_a: int
) -> None:
    """ON DELETE SET NULL on referral_id preserves the import row as
    provenance even if the referral it created is later hard-deleted."""
    patient = storage.create_patient(scope_a, first_name="Jane", last_name="Doe")
    referral = storage.create_referral(scope_a, patient_id=patient.id)
    r = storage.add_csv_import_row(scope_a, import_a, row_index=1, status="committed")
    assert r is not None
    storage.update_csv_import_row(scope_a, import_a, r.id, referral_id=referral.id)

    storage._conn.execute("DELETE FROM referrals WHERE id = ?", (referral.id,))
    storage._conn.commit()

    # Import row still exists, referral_id cleared.
    refreshed = storage._conn.execute(
        "SELECT * FROM csv_import_rows WHERE id = ?", (r.id,)
    ).fetchone()
    assert refreshed is not None
    assert refreshed["referral_id"] is None
    assert refreshed["status"] == "committed"  # unchanged


def test_user_delete_cascades_imports(storage: Storage) -> None:
    uid = storage.create_user("doomed@csv.com", "hashed")
    scope = Scope(user_id=uid)
    imp = storage.create_csv_import(scope, original_filename="doomed.csv")
    storage.add_csv_import_row(scope, imp.id, row_index=1)

    storage._conn.execute("DELETE FROM users WHERE id = ?", (uid,))
    storage._conn.commit()

    # CASCADE on both the import and the row.
    assert (
        storage._conn.execute(
            "SELECT count(*) AS n FROM csv_imports WHERE id = ?", (imp.id,)
        ).fetchone()["n"]
        == 0
    )
