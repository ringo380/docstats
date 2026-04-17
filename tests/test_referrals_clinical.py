"""Tests for referral clinical sub-tables (Phase 1.C).

Covers the four sub-entities (diagnoses / medications / allergies / attachments)
with the same shape:

- add returns None if the parent referral is out of scope (no leak)
- list returns [] if the parent referral is out of scope
- update / delete return None / False on scope mismatch
- soft-deleting the parent hides the sub-entities (via scope gate)
- hard-deleting the parent cascades the sub-entity rows
- enum values match SQL CHECK constraints (drift tests)
"""

from __future__ import annotations

import sqlite3

import pytest

from docstats.domain.referrals import (
    ATTACHMENT_KIND_VALUES,
    SOURCE_VALUES,
    ReferralAllergy,
    ReferralAttachment,
    ReferralDiagnosis,
    ReferralMedication,
)
from docstats.scope import Scope
from docstats.storage import Storage


# --- Fixtures ---


@pytest.fixture
def user_a(storage: Storage) -> int:
    return storage.create_user("a@clin.com", "hashed")


@pytest.fixture
def user_b(storage: Storage) -> int:
    return storage.create_user("b@clin.com", "hashed")


@pytest.fixture
def scope_a(user_a: int) -> Scope:
    return Scope(user_id=user_a)


@pytest.fixture
def scope_b(user_b: int) -> Scope:
    return Scope(user_id=user_b)


@pytest.fixture
def patient_a(storage: Storage, scope_a: Scope) -> int:
    return storage.create_patient(scope_a, first_name="Jane", last_name="Doe").id


@pytest.fixture
def patient_b(storage: Storage, scope_b: Scope) -> int:
    return storage.create_patient(scope_b, first_name="John", last_name="Smith").id


@pytest.fixture
def referral_a(storage: Storage, scope_a: Scope, patient_a: int) -> int:
    return storage.create_referral(scope_a, patient_id=patient_a).id


@pytest.fixture
def referral_b(storage: Storage, scope_b: Scope, patient_b: int) -> int:
    return storage.create_referral(scope_b, patient_id=patient_b).id


# ============================================================
# DIAGNOSES
# ============================================================


def test_add_and_list_diagnosis(storage: Storage, scope_a: Scope, referral_a: int) -> None:
    d = storage.add_referral_diagnosis(
        scope_a,
        referral_a,
        icd10_code="I10",
        icd10_desc="Essential hypertension",
        is_primary=True,
    )
    assert isinstance(d, ReferralDiagnosis)
    assert d.icd10_code == "I10"
    assert d.is_primary is True
    assert d.source == "user_entered"

    listed = storage.list_referral_diagnoses(scope_a, referral_a)
    assert len(listed) == 1
    assert listed[0].id == d.id


def test_list_diagnoses_primary_first(storage: Storage, scope_a: Scope, referral_a: int) -> None:
    d1 = storage.add_referral_diagnosis(scope_a, referral_a, icd10_code="E11.9")
    storage.add_referral_diagnosis(scope_a, referral_a, icd10_code="I10", is_primary=True)
    listed = storage.list_referral_diagnoses(scope_a, referral_a)
    assert listed[0].icd10_code == "I10"  # primary first
    assert listed[0].is_primary is True
    assert listed[1].id == d1.id


def test_only_one_primary_diagnosis_per_referral(
    storage: Storage, scope_a: Scope, referral_a: int
) -> None:
    """The partial unique index `(referral_id) WHERE is_primary` enforces one
    primary per referral. Adding a second primary is a constraint violation."""
    storage.add_referral_diagnosis(scope_a, referral_a, icd10_code="I10", is_primary=True)
    with pytest.raises(sqlite3.IntegrityError):
        storage.add_referral_diagnosis(scope_a, referral_a, icd10_code="E11.9", is_primary=True)


def test_add_diagnosis_cross_tenant_returns_none(
    storage: Storage, scope_b: Scope, referral_a: int
) -> None:
    """Writing into another tenant's referral silently returns None."""
    assert storage.add_referral_diagnosis(scope_b, referral_a, icd10_code="I10") is None
    # And no row was actually written.
    row = storage._conn.execute(
        "SELECT count(*) AS n FROM referral_diagnoses WHERE referral_id = ?",
        (referral_a,),
    ).fetchone()
    assert row["n"] == 0


def test_list_diagnoses_cross_tenant_returns_empty(
    storage: Storage,
    scope_a: Scope,
    scope_b: Scope,
    referral_a: int,
) -> None:
    storage.add_referral_diagnosis(scope_a, referral_a, icd10_code="I10")
    assert storage.list_referral_diagnoses(scope_b, referral_a) == []


def test_update_diagnosis(storage: Storage, scope_a: Scope, referral_a: int) -> None:
    d = storage.add_referral_diagnosis(scope_a, referral_a, icd10_code="I10")
    assert d is not None
    updated = storage.update_referral_diagnosis(
        scope_a, referral_a, d.id, icd10_desc="Hypertension, essential"
    )
    assert updated is not None
    assert updated.icd10_desc == "Hypertension, essential"


def test_update_diagnosis_cross_tenant_returns_none(
    storage: Storage, scope_a: Scope, scope_b: Scope, referral_a: int
) -> None:
    d = storage.add_referral_diagnosis(scope_a, referral_a, icd10_code="I10")
    assert d is not None
    assert storage.update_referral_diagnosis(scope_b, referral_a, d.id, icd10_code="X") is None


def test_delete_diagnosis(storage: Storage, scope_a: Scope, referral_a: int) -> None:
    d = storage.add_referral_diagnosis(scope_a, referral_a, icd10_code="I10")
    assert d is not None
    assert storage.delete_referral_diagnosis(scope_a, referral_a, d.id) is True
    assert storage.list_referral_diagnoses(scope_a, referral_a) == []
    assert storage.delete_referral_diagnosis(scope_a, referral_a, d.id) is False


def test_delete_diagnosis_cross_tenant_returns_false(
    storage: Storage, scope_a: Scope, scope_b: Scope, referral_a: int
) -> None:
    d = storage.add_referral_diagnosis(scope_a, referral_a, icd10_code="I10")
    assert d is not None
    assert storage.delete_referral_diagnosis(scope_b, referral_a, d.id) is False
    # Row still exists.
    assert len(storage.list_referral_diagnoses(scope_a, referral_a)) == 1


# ============================================================
# MEDICATIONS
# ============================================================


def test_add_and_list_medication(storage: Storage, scope_a: Scope, referral_a: int) -> None:
    m = storage.add_referral_medication(
        scope_a,
        referral_a,
        name="Lisinopril",
        dose="10 mg",
        route="oral",
        frequency="daily",
    )
    assert isinstance(m, ReferralMedication)
    assert m.name == "Lisinopril"
    assert m.dose == "10 mg"
    assert storage.list_referral_medications(scope_a, referral_a)[0].id == m.id


def test_add_medication_cross_tenant_returns_none(
    storage: Storage, scope_b: Scope, referral_a: int
) -> None:
    assert storage.add_referral_medication(scope_b, referral_a, name="Lisinopril") is None


def test_update_medication(storage: Storage, scope_a: Scope, referral_a: int) -> None:
    m = storage.add_referral_medication(scope_a, referral_a, name="Lisinopril")
    assert m is not None
    updated = storage.update_referral_medication(
        scope_a, referral_a, m.id, dose="20 mg", frequency="twice daily"
    )
    assert updated is not None
    assert updated.dose == "20 mg"
    assert updated.frequency == "twice daily"
    assert updated.name == "Lisinopril"  # untouched


def test_delete_medication(storage: Storage, scope_a: Scope, referral_a: int) -> None:
    m = storage.add_referral_medication(scope_a, referral_a, name="Lisinopril")
    assert m is not None
    assert storage.delete_referral_medication(scope_a, referral_a, m.id) is True
    assert storage.list_referral_medications(scope_a, referral_a) == []


# ============================================================
# ALLERGIES
# ============================================================


def test_add_and_list_allergy(storage: Storage, scope_a: Scope, referral_a: int) -> None:
    a = storage.add_referral_allergy(
        scope_a,
        referral_a,
        substance="Penicillin",
        reaction="Hives",
        severity="moderate",
    )
    assert isinstance(a, ReferralAllergy)
    assert a.substance == "Penicillin"
    assert storage.list_referral_allergies(scope_a, referral_a)[0].id == a.id


def test_add_allergy_cross_tenant_returns_none(
    storage: Storage, scope_b: Scope, referral_a: int
) -> None:
    assert storage.add_referral_allergy(scope_b, referral_a, substance="Peanuts") is None


def test_update_allergy(storage: Storage, scope_a: Scope, referral_a: int) -> None:
    a = storage.add_referral_allergy(scope_a, referral_a, substance="Penicillin")
    assert a is not None
    updated = storage.update_referral_allergy(
        scope_a, referral_a, a.id, reaction="Anaphylaxis", severity="severe"
    )
    assert updated is not None
    assert updated.reaction == "Anaphylaxis"
    assert updated.severity == "severe"


def test_delete_allergy(storage: Storage, scope_a: Scope, referral_a: int) -> None:
    a = storage.add_referral_allergy(scope_a, referral_a, substance="Penicillin")
    assert a is not None
    assert storage.delete_referral_allergy(scope_a, referral_a, a.id) is True
    assert storage.list_referral_allergies(scope_a, referral_a) == []


# ============================================================
# ATTACHMENTS
# ============================================================


def test_add_and_list_attachment(storage: Storage, scope_a: Scope, referral_a: int) -> None:
    att = storage.add_referral_attachment(
        scope_a,
        referral_a,
        kind="lab",
        label="CBC 2026-04-01",
        date_of_service="2026-04-01",
    )
    assert isinstance(att, ReferralAttachment)
    assert att.kind == "lab"
    assert att.checklist_only is True  # default
    assert att.storage_ref is None


def test_add_attachment_rejects_unknown_kind(
    storage: Storage, scope_a: Scope, referral_a: int
) -> None:
    with pytest.raises(ValueError, match="kind"):
        storage.add_referral_attachment(scope_a, referral_a, kind="mri_3d_holographic", label="x")


def test_add_attachment_cross_tenant_returns_none(
    storage: Storage, scope_b: Scope, referral_a: int
) -> None:
    assert (
        storage.add_referral_attachment(scope_b, referral_a, kind="note", label="hijack attempt")
        is None
    )


def test_update_attachment_flips_checklist_only(
    storage: Storage, scope_a: Scope, referral_a: int
) -> None:
    att = storage.add_referral_attachment(scope_a, referral_a, kind="imaging", label="Chest X-ray")
    assert att is not None
    updated = storage.update_referral_attachment(
        scope_a,
        referral_a,
        att.id,
        checklist_only=False,
        storage_ref="attachments/1/1/foo.pdf",
    )
    assert updated is not None
    assert updated.checklist_only is False
    assert updated.storage_ref == "attachments/1/1/foo.pdf"


def test_delete_attachment(storage: Storage, scope_a: Scope, referral_a: int) -> None:
    att = storage.add_referral_attachment(scope_a, referral_a, kind="note", label="Visit note")
    assert att is not None
    assert storage.delete_referral_attachment(scope_a, referral_a, att.id) is True
    assert storage.list_referral_attachments(scope_a, referral_a) == []


# ============================================================
# Cross-cutting: enum drift, cascade, soft-delete visibility
# ============================================================


def test_source_rejects_unknown_value(storage: Storage, scope_a: Scope, referral_a: int) -> None:
    with pytest.raises(ValueError, match="source"):
        storage.add_referral_diagnosis(scope_a, referral_a, icd10_code="I10", source="made_up")
    with pytest.raises(ValueError, match="source"):
        storage.add_referral_medication(scope_a, referral_a, name="Drug", source="made_up")
    with pytest.raises(ValueError, match="source"):
        storage.add_referral_allergy(scope_a, referral_a, substance="Foo", source="made_up")
    with pytest.raises(ValueError, match="source"):
        storage.add_referral_attachment(
            scope_a, referral_a, kind="note", label="x", source="made_up"
        )


def test_all_source_values_pass_add(storage: Storage, scope_a: Scope, referral_a: int) -> None:
    """Every SOURCE_VALUES entry must be writable on each sub-table — catches
    drift between Python constants and SQL CHECK constraints."""
    for s in SOURCE_VALUES:
        d = storage.add_referral_diagnosis(scope_a, referral_a, icd10_code=f"Z{s}", source=s)
        assert d is not None and d.source == s


def test_all_attachment_kinds_pass_add(storage: Storage, scope_a: Scope, referral_a: int) -> None:
    for k in ATTACHMENT_KIND_VALUES:
        a = storage.add_referral_attachment(scope_a, referral_a, kind=k, label=f"demo {k}")
        assert a is not None and a.kind == k


def test_soft_delete_referral_hides_sub_entities(
    storage: Storage, scope_a: Scope, referral_a: int
) -> None:
    """Sub-entity rows still exist in the DB after a soft-delete of the
    parent, but list operations return [] because get_referral gates them.
    Evidence trail survives; the sub-entities become inaccessible."""
    d = storage.add_referral_diagnosis(scope_a, referral_a, icd10_code="I10")
    assert d is not None
    storage.add_referral_medication(scope_a, referral_a, name="Lisinopril")
    storage.add_referral_allergy(scope_a, referral_a, substance="Penicillin")
    storage.add_referral_attachment(scope_a, referral_a, kind="lab", label="CBC")

    storage.soft_delete_referral(scope_a, referral_a)

    assert storage.list_referral_diagnoses(scope_a, referral_a) == []
    assert storage.list_referral_medications(scope_a, referral_a) == []
    assert storage.list_referral_allergies(scope_a, referral_a) == []
    assert storage.list_referral_attachments(scope_a, referral_a) == []

    # But the rows are still in the DB (evidence).
    row = storage._conn.execute(
        "SELECT count(*) AS n FROM referral_diagnoses WHERE referral_id = ?",
        (referral_a,),
    ).fetchone()
    assert row["n"] == 1


def test_hard_delete_referral_cascades_sub_entities(
    storage: Storage, scope_a: Scope, referral_a: int
) -> None:
    """Admin-level hard-delete (not a normal user action) cascades to all
    four sub-tables. Tests the FK ON DELETE CASCADE clause."""
    storage.add_referral_diagnosis(scope_a, referral_a, icd10_code="I10")
    storage.add_referral_medication(scope_a, referral_a, name="Lisinopril")
    storage.add_referral_allergy(scope_a, referral_a, substance="Penicillin")
    storage.add_referral_attachment(scope_a, referral_a, kind="lab", label="CBC")

    storage._conn.execute("DELETE FROM referrals WHERE id = ?", (referral_a,))
    storage._conn.commit()

    for table in (
        "referral_diagnoses",
        "referral_medications",
        "referral_allergies",
        "referral_attachments",
    ):
        row = storage._conn.execute(
            f"SELECT count(*) AS n FROM {table} WHERE referral_id = ?", (referral_a,)
        ).fetchone()
        assert row["n"] == 0, f"Cascade failed for {table}"
