"""End-to-end scope isolation sweep (Phase 1.G).

Proves the scope-enforcement invariant holds ACROSS the entire Phase 1
entity graph: a user acting in tenant A cannot read, modify, or delete
any row owned by tenant B — regardless of whether that row is a
first-class scoped entity or hangs transitively off one. Individual
test modules pin each entity's isolation in isolation; this module's
job is the composed whole.

Two "tenants" are built with a full-stack payload each:

    tenant A (solo user)           tenant B (org)
    ├── patient                    ├── patient
    ├── insurance plan             ├── insurance plan
    ├── csv_import                 ├── csv_import
    │   └── csv_import_row         │   └── csv_import_row
    └── referral                   └── referral
        ├── diagnosis (primary)        ├── diagnosis (primary)
        ├── medication                 ├── medication
        ├── allergy                    ├── allergy
        ├── attachment                 ├── attachment
        ├── response                   ├── response
        └── referral_event             └── referral_event

Then every plausible cross-tenant access path is attempted. If any of
them leaks data, this suite catches it.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from docstats.domain.referrals import transition_allowed
from docstats.scope import Scope
from docstats.storage import Storage


@dataclass(frozen=True)
class Tenant:
    """All the IDs a cross-tenant attack would need to know."""

    scope: Scope
    user_id: int
    org_id: int | None
    patient_id: int
    referral_id: int
    diagnosis_id: int
    medication_id: int
    allergy_id: int
    attachment_id: int
    response_id: int
    plan_id: int
    import_id: int
    import_row_id: int


@pytest.fixture
def tenant_a(storage: Storage) -> Tenant:
    """Solo-mode tenant with a full-stack payload."""
    uid = storage.create_user("alice@tenant-a.com", "hashed")
    scope = Scope(user_id=uid)
    return _build_tenant(storage, scope, uid, None, name_prefix="A")


@pytest.fixture
def tenant_b(storage: Storage) -> Tenant:
    """Org-mode tenant with a full-stack payload, to test both modes."""
    uid = storage.create_user("bob@tenant-b.com", "hashed")
    org = storage.create_organization(name="Tenant B Clinic", slug="tenant-b")
    storage.create_membership(organization_id=org.id, user_id=uid, role="owner")
    scope = Scope(user_id=uid, organization_id=org.id, membership_role="owner")
    return _build_tenant(storage, scope, uid, org.id, name_prefix="B")


def _build_tenant(
    storage: Storage,
    scope: Scope,
    user_id: int,
    org_id: int | None,
    *,
    name_prefix: str,
) -> Tenant:
    patient = storage.create_patient(
        scope,
        first_name=f"{name_prefix}Patient",
        last_name="Doe",
        date_of_birth="1980-01-01",
        mrn=f"{name_prefix}-MRN-1" if org_id else None,
        created_by_user_id=user_id,
    )
    plan = storage.create_insurance_plan(
        scope,
        payer_name=f"{name_prefix}-Payer",
        plan_name="HMO",
        plan_type="hmo",
    )
    referral = storage.create_referral(
        scope,
        patient_id=patient.id,
        reason=f"{name_prefix} reason",
        urgency="routine",
        created_by_user_id=user_id,
    )
    diagnosis = storage.add_referral_diagnosis(
        scope,
        referral.id,
        icd10_code="I10",
        icd10_desc="Hypertension",
        is_primary=True,
    )
    medication = storage.add_referral_medication(
        scope, referral.id, name="Lisinopril", dose="10 mg"
    )
    allergy = storage.add_referral_allergy(
        scope, referral.id, substance="Penicillin", reaction="Rash"
    )
    attachment = storage.add_referral_attachment(
        scope, referral.id, kind="lab", label=f"{name_prefix} CBC"
    )
    response = storage.record_referral_response(
        scope,
        referral.id,
        appointment_date="2026-05-01",
        received_via="fax",
        recorded_by_user_id=user_id,
    )
    imp = storage.create_csv_import(
        scope,
        original_filename=f"{name_prefix}.csv",
        uploaded_by_user_id=user_id,
        row_count=1,
    )
    row = storage.add_csv_import_row(
        scope, imp.id, row_index=1, raw_json={"col": f"{name_prefix}-val"}
    )

    # None-asserts document our assumption that the happy-path setup worked.
    assert diagnosis is not None
    assert medication is not None
    assert allergy is not None
    assert attachment is not None
    assert response is not None
    assert row is not None

    return Tenant(
        scope=scope,
        user_id=user_id,
        org_id=org_id,
        patient_id=patient.id,
        referral_id=referral.id,
        diagnosis_id=diagnosis.id,
        medication_id=medication.id,
        allergy_id=allergy.id,
        attachment_id=attachment.id,
        response_id=response.id,
        plan_id=plan.id,
        import_id=imp.id,
        import_row_id=row.id,
    )


# ============================================================
# READ ISOLATION — every get/list from A must not see B's data
# ============================================================


def test_get_operations_cannot_reach_other_tenant(
    storage: Storage, tenant_a: Tenant, tenant_b: Tenant
) -> None:
    """Every get-by-id call from tenant A with tenant B's ids returns None."""
    a = tenant_a.scope
    b = tenant_b
    assert storage.get_patient(a, b.patient_id) is None
    assert storage.get_referral(a, b.referral_id) is None
    assert storage.get_insurance_plan(a, b.plan_id) is None
    assert storage.get_csv_import(a, b.import_id) is None


def test_list_operations_only_return_own_tenants_rows(
    storage: Storage, tenant_a: Tenant, tenant_b: Tenant
) -> None:
    """List-all calls from A return only A's rows; same for B."""
    a = tenant_a.scope
    b = tenant_b.scope

    a_patients = storage.list_patients(a)
    assert len(a_patients) == 1
    assert a_patients[0].id == tenant_a.patient_id

    b_patients = storage.list_patients(b)
    assert len(b_patients) == 1
    assert b_patients[0].id == tenant_b.patient_id

    a_referrals = storage.list_referrals(a)
    assert len(a_referrals) == 1
    assert a_referrals[0].id == tenant_a.referral_id

    b_referrals = storage.list_referrals(b)
    assert len(b_referrals) == 1
    assert b_referrals[0].id == tenant_b.referral_id

    a_plans = storage.list_insurance_plans(a)
    assert {p.id for p in a_plans} == {tenant_a.plan_id}

    a_imports = storage.list_csv_imports(a)
    assert {i.id for i in a_imports} == {tenant_a.import_id}


def test_scope_transitive_entities_not_visible_cross_tenant(
    storage: Storage, tenant_a: Tenant, tenant_b: Tenant
) -> None:
    """Clinical sub-entities + events + responses hang off referrals and
    must inherit scope isolation — list from A using B's referral_id
    returns []."""
    a = tenant_a.scope
    assert storage.list_referral_diagnoses(a, tenant_b.referral_id) == []
    assert storage.list_referral_medications(a, tenant_b.referral_id) == []
    assert storage.list_referral_allergies(a, tenant_b.referral_id) == []
    assert storage.list_referral_attachments(a, tenant_b.referral_id) == []
    assert storage.list_referral_responses(a, tenant_b.referral_id) == []
    assert storage.list_referral_events(a, tenant_b.referral_id) == []


def test_csv_import_rows_not_visible_cross_tenant(
    storage: Storage, tenant_a: Tenant, tenant_b: Tenant
) -> None:
    a = tenant_a.scope
    assert storage.list_csv_import_rows(a, tenant_b.import_id) == []


# ============================================================
# WRITE ISOLATION — every update/delete from A must not touch B's data
# ============================================================


def _snapshot_tenant_b_counts(storage: Storage, b: Tenant) -> dict[str, int]:
    """Count all of B's rows for post-attack comparison."""
    return {
        "patients": len(storage.list_patients(b.scope)),
        "referrals": len(storage.list_referrals(b.scope)),
        "diagnoses": len(storage.list_referral_diagnoses(b.scope, b.referral_id)),
        "medications": len(storage.list_referral_medications(b.scope, b.referral_id)),
        "allergies": len(storage.list_referral_allergies(b.scope, b.referral_id)),
        "attachments": len(storage.list_referral_attachments(b.scope, b.referral_id)),
        "responses": len(storage.list_referral_responses(b.scope, b.referral_id)),
        "events": len(storage.list_referral_events(b.scope, b.referral_id)),
        "plans": len(storage.list_insurance_plans(b.scope)),
        "imports": len(storage.list_csv_imports(b.scope)),
        "rows": len(storage.list_csv_import_rows(b.scope, b.import_id)),
    }


def test_update_operations_cannot_touch_other_tenant(
    storage: Storage, tenant_a: Tenant, tenant_b: Tenant
) -> None:
    a = tenant_a.scope
    b = tenant_b
    before = _snapshot_tenant_b_counts(storage, b)

    # Every update call from A aimed at B returns None (no row updated).
    assert storage.update_patient(a, b.patient_id, first_name="HIJACK") is None
    assert storage.update_referral(a, b.referral_id, reason="HIJACK") is None
    assert storage.set_referral_status(a, b.referral_id, "cancelled") is None
    assert (
        storage.update_referral_diagnosis(a, b.referral_id, b.diagnosis_id, icd10_code="Z00")
        is None
    )
    assert (
        storage.update_referral_medication(a, b.referral_id, b.medication_id, dose="99 mg") is None
    )
    assert (
        storage.update_referral_allergy(a, b.referral_id, b.allergy_id, reaction="HIJACK") is None
    )
    assert (
        storage.update_referral_attachment(a, b.referral_id, b.attachment_id, label="HIJACK")
        is None
    )
    assert (
        storage.update_referral_response(
            a, b.referral_id, b.response_id, recommendations_text="HIJACK"
        )
        is None
    )
    assert storage.update_insurance_plan(a, b.plan_id, payer_name="HIJACK") is None
    assert storage.update_csv_import(a, b.import_id, status="failed") is None
    assert (
        storage.update_csv_import_row(a, b.import_id, b.import_row_id, status="committed") is None
    )

    # Row counts unchanged and B's fields still pristine.
    after = _snapshot_tenant_b_counts(storage, b)
    assert before == after

    b_patient = storage.get_patient(b.scope, b.patient_id)
    assert b_patient is not None
    assert b_patient.first_name == "BPatient"

    b_referral = storage.get_referral(b.scope, b.referral_id)
    assert b_referral is not None
    assert b_referral.reason == "B reason"
    assert b_referral.status == "draft"


def test_delete_operations_cannot_touch_other_tenant(
    storage: Storage, tenant_a: Tenant, tenant_b: Tenant
) -> None:
    a = tenant_a.scope
    b = tenant_b
    before = _snapshot_tenant_b_counts(storage, b)

    assert storage.soft_delete_patient(a, b.patient_id) is False
    assert storage.soft_delete_referral(a, b.referral_id) is False
    assert storage.delete_referral_diagnosis(a, b.referral_id, b.diagnosis_id) is False
    assert storage.delete_referral_medication(a, b.referral_id, b.medication_id) is False
    assert storage.delete_referral_allergy(a, b.referral_id, b.allergy_id) is False
    assert storage.delete_referral_attachment(a, b.referral_id, b.attachment_id) is False
    assert storage.delete_referral_response(a, b.referral_id, b.response_id) is False
    assert storage.soft_delete_insurance_plan(a, b.plan_id) is False
    assert storage.delete_csv_import(a, b.import_id) is False
    assert storage.delete_csv_import_row(a, b.import_id, b.import_row_id) is False

    after = _snapshot_tenant_b_counts(storage, b)
    assert before == after


def test_cross_tenant_add_to_referral_returns_none_no_write(
    storage: Storage, tenant_a: Tenant, tenant_b: Tenant
) -> None:
    """Trying to add a sub-entity row under another tenant's referral
    returns None AND writes no row (silent rejection, no leak)."""
    a = tenant_a.scope
    before = _snapshot_tenant_b_counts(storage, tenant_b)

    assert storage.add_referral_diagnosis(a, tenant_b.referral_id, icd10_code="Z99") is None
    assert storage.add_referral_medication(a, tenant_b.referral_id, name="Ghostpill") is None
    assert storage.add_referral_allergy(a, tenant_b.referral_id, substance="Ghost") is None
    assert (
        storage.add_referral_attachment(a, tenant_b.referral_id, kind="note", label="Ghost") is None
    )
    assert storage.record_referral_response(a, tenant_b.referral_id, received_via="manual") is None
    assert (
        storage.record_referral_event(
            a, tenant_b.referral_id, event_type="note_added", note="Ghost"
        )
        is None
    )
    assert storage.add_csv_import_row(a, tenant_b.import_id, row_index=999) is None

    after = _snapshot_tenant_b_counts(storage, tenant_b)
    assert before == after


def test_cross_tenant_patient_fk_forgery_rejected(
    storage: Storage, tenant_a: Tenant, tenant_b: Tenant
) -> None:
    """create_referral must refuse a cross-scope patient_id — tenant A
    cannot create a referral pointing at tenant B's patient (even though
    that would otherwise be an SQL-valid row with A's scope columns)."""
    with pytest.raises(ValueError, match="not found in scope"):
        storage.create_referral(tenant_a.scope, patient_id=tenant_b.patient_id)

    # B's referral count unchanged.
    b_referrals = storage.list_referrals(tenant_b.scope)
    assert len(b_referrals) == 1


# ============================================================
# HAPPY-PATH SANITY — internal operations still work inside one tenant
# ============================================================


def test_within_tenant_full_stack_works(storage: Storage, tenant_a: Tenant) -> None:
    """Sanity: after all the cross-tenant attempts above, the tenant's
    own operations still work — no global state corruption."""
    a = tenant_a.scope

    # Read every entity in tenant A's scope.
    assert storage.get_patient(a, tenant_a.patient_id) is not None
    assert storage.get_referral(a, tenant_a.referral_id) is not None
    assert storage.get_insurance_plan(a, tenant_a.plan_id) is not None
    assert storage.get_csv_import(a, tenant_a.import_id) is not None
    assert len(storage.list_referral_diagnoses(a, tenant_a.referral_id)) == 1
    assert len(storage.list_referral_medications(a, tenant_a.referral_id)) == 1
    assert len(storage.list_referral_allergies(a, tenant_a.referral_id)) == 1
    assert len(storage.list_referral_attachments(a, tenant_a.referral_id)) == 1
    assert len(storage.list_referral_responses(a, tenant_a.referral_id)) == 1
    # Event log has the auto-seeded "created" event.
    events = storage.list_referral_events(a, tenant_a.referral_id)
    assert any(e.event_type == "created" for e in events)

    # Transition the referral through a realistic flow.
    assert transition_allowed("draft", "ready")
    assert storage.set_referral_status(a, tenant_a.referral_id, "ready") is not None
    assert transition_allowed("ready", "sent")
    assert storage.set_referral_status(a, tenant_a.referral_id, "sent") is not None
    final = storage.set_referral_status(a, tenant_a.referral_id, "scheduled")
    assert final is not None
    assert final.status == "scheduled"

    # Add another diagnosis to the same referral — relative ordering works.
    second = storage.add_referral_diagnosis(
        a, tenant_a.referral_id, icd10_code="E11.9", is_primary=False
    )
    assert second is not None
    listed = storage.list_referral_diagnoses(a, tenant_a.referral_id)
    assert len(listed) == 2
    # Primary (I10) comes first; new row (E11.9) second.
    assert listed[0].is_primary is True
    assert listed[0].icd10_code == "I10"


def test_org_scope_and_solo_scope_both_usable_in_same_db(
    storage: Storage, tenant_a: Tenant, tenant_b: Tenant
) -> None:
    """Sanity check that solo-mode (A) and org-mode (B) coexist cleanly
    in the same DB with no state leaking across — the core promise of
    the dual-mode Scope primitive."""
    assert tenant_a.scope.is_solo
    assert tenant_b.scope.is_org
    assert tenant_a.org_id is None
    assert tenant_b.org_id is not None

    # Totals across tenants add up to the full set.
    # (Using the Storage internal connection here is deliberate — the
    # scope-filtered list_ methods cannot give us a raw count by design.)
    raw_patient_count = storage._conn.execute(
        "SELECT count(*) FROM patients WHERE deleted_at IS NULL"
    ).fetchone()[0]
    assert raw_patient_count == 2

    raw_referral_count = storage._conn.execute(
        "SELECT count(*) FROM referrals WHERE deleted_at IS NULL"
    ).fetchone()[0]
    assert raw_referral_count == 2
