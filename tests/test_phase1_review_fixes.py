"""Regression tests for the Phase 1 code-review findings.

Each test pins a specific bug that slipped through PRs #84, #88, #89, #90,
#91, #92, #93. If any of these fail, the retrospective fix has regressed.
"""

from __future__ import annotations

import pytest

from docstats.domain.seed import seed_platform_defaults
from docstats.scope import Scope
from docstats.storage import Storage


# --- Fixtures ---


@pytest.fixture
def user_a(storage: Storage) -> int:
    return storage.create_user("alice@review-fixes.test", "x")


@pytest.fixture
def user_b(storage: Storage) -> int:
    return storage.create_user("bob@review-fixes.test", "x")


@pytest.fixture
def scope_a(user_a: int) -> Scope:
    return Scope(user_id=user_a)


@pytest.fixture
def scope_b(user_b: int) -> Scope:
    return Scope(user_id=user_b)


@pytest.fixture
def patient_a(storage: Storage, scope_a: Scope) -> int:
    return storage.create_patient(scope_a, first_name="Alice", last_name="A").id


@pytest.fixture
def patient_b(storage: Storage, scope_b: Scope) -> int:
    return storage.create_patient(scope_b, first_name="Bob", last_name="B").id


@pytest.fixture
def referral_a(storage: Storage, scope_a: Scope, patient_a: int) -> int:
    return storage.create_referral(scope_a, patient_id=patient_a).id


@pytest.fixture
def referral_b(storage: Storage, scope_b: Scope, patient_b: int) -> int:
    return storage.create_referral(scope_b, patient_id=patient_b).id


# --- Review finding #1: csv_import_row.referral_id cross-tenant FK forgery ---


def test_update_csv_import_row_rejects_cross_scope_referral_id(
    storage: Storage,
    scope_a: Scope,
    scope_b: Scope,
    referral_b: int,
) -> None:
    """A caller in scope A cannot link an import row to a referral that
    only exists in scope B — even if the import itself is in scope A.
    Mirrors the ``create_referral`` cross-scope patient FK guard.
    """
    imp = storage.create_csv_import(scope_a, original_filename="x.csv")
    row = storage.add_csv_import_row(scope_a, imp.id, row_index=0, raw_json={})
    assert row is not None

    with pytest.raises(ValueError, match="not accessible from the caller's scope"):
        storage.update_csv_import_row(scope_a, imp.id, row.id, referral_id=referral_b)


def test_update_csv_import_row_accepts_in_scope_referral_id(
    storage: Storage,
    scope_a: Scope,
    referral_a: int,
) -> None:
    imp = storage.create_csv_import(scope_a, original_filename="x.csv")
    row = storage.add_csv_import_row(scope_a, imp.id, row_index=0, raw_json={})
    assert row is not None
    updated = storage.update_csv_import_row(scope_a, imp.id, row.id, referral_id=referral_a)
    assert updated is not None
    assert updated.referral_id == referral_a


# --- Review finding #2: payer_plan_id cross-tenant FK forgery ---


def test_create_referral_rejects_cross_scope_payer_plan(
    storage: Storage,
    scope_a: Scope,
    scope_b: Scope,
    patient_a: int,
) -> None:
    """A referral in scope A cannot attach a payer_plan_id that belongs
    to scope B — same risk class as the patient_id scope check.
    """
    plan_b = storage.create_insurance_plan(scope_b, payer_name="Blue Cross", plan_type="ppo")
    with pytest.raises(ValueError, match="not accessible from the caller's scope"):
        storage.create_referral(scope_a, patient_id=patient_a, payer_plan_id=plan_b.id)


def test_update_referral_rejects_cross_scope_payer_plan(
    storage: Storage,
    scope_a: Scope,
    scope_b: Scope,
    referral_a: int,
) -> None:
    plan_b = storage.create_insurance_plan(scope_b, payer_name="Aetna", plan_type="hmo")
    with pytest.raises(ValueError, match="not accessible from the caller's scope"):
        storage.update_referral(scope_a, referral_a, payer_plan_id=plan_b.id)


def test_create_referral_accepts_in_scope_payer_plan(
    storage: Storage,
    scope_a: Scope,
    patient_a: int,
) -> None:
    plan = storage.create_insurance_plan(scope_a, payer_name="Kaiser", plan_type="hmo")
    ref = storage.create_referral(scope_a, patient_id=patient_a, payer_plan_id=plan.id)
    assert ref.payer_plan_id == plan.id


# --- Review finding #3: seed overwrite version + None-clear (covered in test_seed.py) ---


# --- Review finding #4: headline diagnosis sync ---


def test_add_primary_diagnosis_syncs_headline(
    storage: Storage, scope_a: Scope, referral_a: int
) -> None:
    """Adding a diagnosis with is_primary=True must push its code/desc up
    onto the parent referral's denormalized headline columns.
    """
    storage.add_referral_diagnosis(
        scope_a,
        referral_a,
        icd10_code="I10",
        icd10_desc="Essential hypertension",
        is_primary=True,
    )
    ref = storage.get_referral(scope_a, referral_a)
    assert ref is not None
    assert ref.diagnosis_primary_icd == "I10"
    assert ref.diagnosis_primary_text == "Essential hypertension"


def test_add_nonprimary_diagnosis_does_not_touch_headline(
    storage: Storage, scope_a: Scope, patient_a: int
) -> None:
    """If a referral was created with manually-set headline fields and no
    sub-table rows exist yet, adding a non-primary diagnosis must NOT
    clobber the headline.
    """
    ref = storage.create_referral(
        scope_a,
        patient_id=patient_a,
        diagnosis_primary_icd="E11.9",
        diagnosis_primary_text="T2DM",
    )
    storage.add_referral_diagnosis(scope_a, ref.id, icd10_code="Z79.4", is_primary=False)
    after = storage.get_referral(scope_a, ref.id)
    assert after is not None
    assert after.diagnosis_primary_icd == "E11.9"
    assert after.diagnosis_primary_text == "T2DM"


def test_update_primary_diagnosis_syncs_headline(
    storage: Storage, scope_a: Scope, referral_a: int
) -> None:
    d = storage.add_referral_diagnosis(
        scope_a, referral_a, icd10_code="I10", icd10_desc="HTN", is_primary=True
    )
    assert d is not None
    storage.update_referral_diagnosis(
        scope_a, referral_a, d.id, icd10_code="I10.9", icd10_desc="HTN, unspecified"
    )
    ref = storage.get_referral(scope_a, referral_a)
    assert ref is not None
    assert ref.diagnosis_primary_icd == "I10.9"
    assert ref.diagnosis_primary_text == "HTN, unspecified"


def test_toggle_primary_off_clears_headline(
    storage: Storage, scope_a: Scope, referral_a: int
) -> None:
    d = storage.add_referral_diagnosis(scope_a, referral_a, icd10_code="I10", is_primary=True)
    assert d is not None
    storage.update_referral_diagnosis(scope_a, referral_a, d.id, is_primary=False)
    ref = storage.get_referral(scope_a, referral_a)
    assert ref is not None
    assert ref.diagnosis_primary_icd is None
    assert ref.diagnosis_primary_text is None


def test_delete_primary_diagnosis_clears_headline(
    storage: Storage, scope_a: Scope, referral_a: int
) -> None:
    d = storage.add_referral_diagnosis(scope_a, referral_a, icd10_code="I10", is_primary=True)
    assert d is not None
    storage.delete_referral_diagnosis(scope_a, referral_a, d.id)
    ref = storage.get_referral(scope_a, referral_a)
    assert ref is not None
    assert ref.diagnosis_primary_icd is None


def test_delete_nonprimary_diagnosis_does_not_touch_headline(
    storage: Storage, scope_a: Scope, referral_a: int
) -> None:
    # Primary row first; it seeds the headline.
    storage.add_referral_diagnosis(scope_a, referral_a, icd10_code="I10", is_primary=True)
    # Secondary row that we'll delete.
    secondary = storage.add_referral_diagnosis(
        scope_a, referral_a, icd10_code="Z79.4", is_primary=False
    )
    assert secondary is not None
    storage.delete_referral_diagnosis(scope_a, referral_a, secondary.id)
    ref = storage.get_referral(scope_a, referral_a)
    assert ref is not None
    assert ref.diagnosis_primary_icd == "I10"  # primary survives


# --- Review finding #5: atomic create_referral ---


def test_create_referral_and_event_in_single_transaction(
    storage: Storage, scope_a: Scope, patient_a: int
) -> None:
    """Creating a referral must insert both the referral row AND its
    ``created`` timeline event as a single atomic transaction. A partial
    write would leave a referral with no events — the append-only timeline
    contract guarantees at least one entry from t=0.
    """
    ref = storage.create_referral(scope_a, patient_id=patient_a)
    events = storage.list_referral_events(scope_a, ref.id)
    assert len(events) == 1
    assert events[0].event_type == "created"
    assert events[0].to_value == "draft"


# --- Review finding: dedicated clear methods ---


def test_clear_referral_field_assigned_to(
    storage: Storage, scope_a: Scope, patient_a: int, user_a: int
) -> None:
    ref = storage.create_referral(scope_a, patient_id=patient_a, assigned_to_user_id=user_a)
    cleared = storage.clear_referral_field(scope_a, ref.id, "assigned_to_user_id")
    assert cleared is not None
    assert cleared.assigned_to_user_id is None


def test_clear_referral_field_rejects_non_clearable(
    storage: Storage, scope_a: Scope, referral_a: int
) -> None:
    """Required columns and scope keys must not be clearable — a typo
    shouldn't silently nuke ``patient_id`` or ``status``.
    """
    with pytest.raises(ValueError, match="not clearable"):
        storage.clear_referral_field(scope_a, referral_a, "patient_id")
    with pytest.raises(ValueError, match="not clearable"):
        storage.clear_referral_field(scope_a, referral_a, "status")


def test_clear_referral_field_scope_isolated(
    storage: Storage, scope_a: Scope, scope_b: Scope, referral_b: int
) -> None:
    """Cross-tenant clear must return None (not raise) so we don't leak
    existence of out-of-scope referrals.
    """
    result = storage.clear_referral_field(scope_a, referral_b, "assigned_to_user_id")
    assert result is None


# --- Review finding: list_*_rules caller contract ---


def test_list_specialty_rules_with_org_returns_both_global_and_override(
    storage: Storage,
) -> None:
    """The caller contract: list_specialty_rules(org_id=X, include_globals=True)
    returns both the platform default AND any org override for the same
    specialty_code as separate rows. The rules engine merges them.
    """
    seed_platform_defaults(storage)
    org = storage.create_organization(name="Test Clinic", slug="test-clinic-a")
    # Add an org override for cardiology.
    storage.create_specialty_rule(
        specialty_code="207RC0000X",
        organization_id=org.id,
        display_name="Cardiology (Test Clinic)",
        source="admin_override",
    )
    rules = storage.list_specialty_rules(organization_id=org.id, include_globals=True)
    cardio_rows = [r for r in rules if r.specialty_code == "207RC0000X"]
    assert len(cardio_rows) == 2
    # Global comes first per the NULLS FIRST ordering.
    assert cardio_rows[0].organization_id is None
    assert cardio_rows[1].organization_id == org.id


def test_list_specialty_rules_without_globals_returns_org_only(
    storage: Storage,
) -> None:
    seed_platform_defaults(storage)
    org = storage.create_organization(name="Test Clinic 2", slug="test-clinic-2")
    storage.create_specialty_rule(
        specialty_code="207RC0000X",
        organization_id=org.id,
        display_name="Cardiology (TC2)",
        source="admin_override",
    )
    rules = storage.list_specialty_rules(organization_id=org.id, include_globals=False)
    # Only the org override, no platform defaults.
    assert all(r.organization_id == org.id for r in rules)
    assert len(rules) == 1
