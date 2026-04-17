"""Tests for reference/config tables (Phase 1.E)."""

from __future__ import annotations

import sqlite3

import pytest

from docstats.domain.reference import (
    PLAN_TYPE_VALUES,
    RULE_SOURCE_VALUES,
    InsurancePlan,
    PayerRule,
    SpecialtyRule,
)
from docstats.scope import Scope, ScopeRequired
from docstats.storage import Storage


# --- Fixtures ---


@pytest.fixture
def user_a(storage: Storage) -> int:
    return storage.create_user("a@ref.com", "hashed")


@pytest.fixture
def user_b(storage: Storage) -> int:
    return storage.create_user("b@ref.com", "hashed")


@pytest.fixture
def scope_a(user_a: int) -> Scope:
    return Scope(user_id=user_a)


@pytest.fixture
def scope_b(user_b: int) -> Scope:
    return Scope(user_id=user_b)


@pytest.fixture
def org_a(storage: Storage, user_a: int) -> int:
    org = storage.create_organization(name="Org A", slug="org-a-ref")
    storage.create_membership(organization_id=org.id, user_id=user_a, role="owner")
    return org.id


@pytest.fixture
def org_b(storage: Storage, user_b: int) -> int:
    org = storage.create_organization(name="Org B", slug="org-b-ref")
    storage.create_membership(organization_id=org.id, user_id=user_b, role="owner")
    return org.id


# ============================================================
# INSURANCE PLANS (scope-owned)
# ============================================================


def test_create_insurance_plan_solo(storage: Storage, scope_a: Scope) -> None:
    p = storage.create_insurance_plan(
        scope_a,
        payer_name="Kaiser Permanente",
        plan_name="HMO Silver",
        plan_type="hmo",
        requires_referral=True,
    )
    assert isinstance(p, InsurancePlan)
    assert p.scope_user_id == scope_a.user_id
    assert p.scope_organization_id is None
    assert p.plan_type == "hmo"
    assert p.requires_referral is True
    assert p.requires_prior_auth is False


def test_create_insurance_plan_rejects_unknown_type(storage: Storage, scope_a: Scope) -> None:
    with pytest.raises(ValueError, match="plan_type"):
        storage.create_insurance_plan(scope_a, payer_name="Mystery", plan_type="barter")


def test_create_insurance_plan_rejects_anonymous_scope(storage: Storage) -> None:
    with pytest.raises(ScopeRequired):
        storage.create_insurance_plan(Scope(), payer_name="Nobody")


def test_all_plan_type_values_writable(storage: Storage, scope_a: Scope) -> None:
    """Every PLAN_TYPE_VALUES entry must pass the SQL CHECK constraint."""
    for pt in PLAN_TYPE_VALUES:
        p = storage.create_insurance_plan(scope_a, payer_name=f"P-{pt}", plan_type=pt)
        assert p.plan_type == pt


def test_list_insurance_plans_scope_filtered(
    storage: Storage, scope_a: Scope, scope_b: Scope
) -> None:
    storage.create_insurance_plan(scope_a, payer_name="A1")
    storage.create_insurance_plan(scope_a, payer_name="A2")
    storage.create_insurance_plan(scope_b, payer_name="B1")

    a_plans = storage.list_insurance_plans(scope_a)
    b_plans = storage.list_insurance_plans(scope_b)
    assert {p.payer_name for p in a_plans} == {"A1", "A2"}
    assert {p.payer_name for p in b_plans} == {"B1"}


def test_get_insurance_plan_cross_tenant_returns_none(
    storage: Storage, scope_a: Scope, scope_b: Scope
) -> None:
    p = storage.create_insurance_plan(scope_a, payer_name="A1")
    assert storage.get_insurance_plan(scope_b, p.id) is None
    assert storage.get_insurance_plan(scope_a, p.id) is not None


def test_update_insurance_plan(storage: Storage, scope_a: Scope) -> None:
    p = storage.create_insurance_plan(scope_a, payer_name="Kaiser", plan_type="hmo")
    updated = storage.update_insurance_plan(
        scope_a, p.id, requires_prior_auth=True, notes="verified 2026-04"
    )
    assert updated is not None
    assert updated.requires_prior_auth is True
    assert updated.notes == "verified 2026-04"
    assert updated.plan_type == "hmo"  # untouched


def test_update_insurance_plan_cross_tenant_returns_none(
    storage: Storage, scope_a: Scope, scope_b: Scope
) -> None:
    p = storage.create_insurance_plan(scope_a, payer_name="Kaiser")
    assert storage.update_insurance_plan(scope_b, p.id, notes="hijack") is None
    fresh = storage.get_insurance_plan(scope_a, p.id)
    assert fresh is not None and fresh.notes is None


def test_soft_delete_insurance_plan(storage: Storage, scope_a: Scope) -> None:
    p = storage.create_insurance_plan(scope_a, payer_name="Ghost")
    assert storage.soft_delete_insurance_plan(scope_a, p.id) is True
    assert storage.get_insurance_plan(scope_a, p.id) is None
    assert storage.list_insurance_plans(scope_a) == []
    assert len(storage.list_insurance_plans(scope_a, include_deleted=True)) == 1
    assert storage.soft_delete_insurance_plan(scope_a, p.id) is False


def test_insurance_plan_check_constraint_on_scope(storage: Storage) -> None:
    """Both scope cols NULL or both set must be rejected by the DB."""
    with pytest.raises(sqlite3.IntegrityError):
        storage._conn.execute(
            "INSERT INTO insurance_plans (scope_user_id, scope_organization_id, payer_name) "
            "VALUES (NULL, NULL, 'Nobody')"
        )
        storage._conn.commit()
    storage._conn.rollback()


# ============================================================
# SPECIALTY RULES (platform default or org override)
# ============================================================


def test_create_global_specialty_rule(storage: Storage) -> None:
    r = storage.create_specialty_rule(
        specialty_code="207R00000X",
        display_name="Internal Medicine",
        required_fields={"fields": ["reason", "clinical_question"]},
        urgency_red_flags={"keywords": ["chest pain", "shortness of breath"]},
    )
    assert isinstance(r, SpecialtyRule)
    assert r.organization_id is None
    assert r.version_id == 1
    assert r.source == "seed"
    assert r.required_fields == {"fields": ["reason", "clinical_question"]}


def test_create_org_specialty_rule(storage: Storage, org_a: int) -> None:
    r = storage.create_specialty_rule(
        specialty_code="207R00000X",
        organization_id=org_a,
        display_name="Internal Med (Org A override)",
        source="admin_override",
    )
    assert r.organization_id == org_a
    assert r.source == "admin_override"


def test_create_specialty_rule_rejects_unknown_source(storage: Storage) -> None:
    with pytest.raises(ValueError, match="source"):
        storage.create_specialty_rule(specialty_code="X", source="made_up")


def test_all_rule_source_values_writable(storage: Storage, org_a: int) -> None:
    for s in RULE_SOURCE_VALUES:
        r = storage.create_specialty_rule(
            specialty_code=f"CODE-{s}", organization_id=org_a, source=s
        )
        assert r.source == s


def test_list_specialty_rules_globals_only(storage: Storage) -> None:
    storage.create_specialty_rule(specialty_code="A")
    storage.create_specialty_rule(specialty_code="B")
    rules = storage.list_specialty_rules()
    assert len(rules) == 2
    assert all(r.organization_id is None for r in rules)


def test_list_specialty_rules_includes_globals_and_org(
    storage: Storage, org_a: int, org_b: int
) -> None:
    storage.create_specialty_rule(specialty_code="G1")
    storage.create_specialty_rule(specialty_code="G2")
    storage.create_specialty_rule(specialty_code="X", organization_id=org_a)
    storage.create_specialty_rule(specialty_code="Y", organization_id=org_b)

    for_a = storage.list_specialty_rules(organization_id=org_a)
    assert len(for_a) == 3
    codes = {r.specialty_code for r in for_a}
    assert codes == {"G1", "G2", "X"}


def test_list_specialty_rules_org_only(storage: Storage, org_a: int) -> None:
    storage.create_specialty_rule(specialty_code="G1")
    storage.create_specialty_rule(specialty_code="X", organization_id=org_a)

    just_org = storage.list_specialty_rules(organization_id=org_a, include_globals=False)
    assert len(just_org) == 1
    assert just_org[0].specialty_code == "X"


def test_specialty_rule_one_global_per_code(storage: Storage) -> None:
    """Partial unique index: only one platform-default row per specialty_code."""
    storage.create_specialty_rule(specialty_code="207R00000X")
    with pytest.raises(sqlite3.IntegrityError):
        storage.create_specialty_rule(specialty_code="207R00000X")


def test_specialty_rule_one_per_org_per_code(storage: Storage, org_a: int) -> None:
    storage.create_specialty_rule(specialty_code="207R00000X", organization_id=org_a)
    with pytest.raises(sqlite3.IntegrityError):
        storage.create_specialty_rule(specialty_code="207R00000X", organization_id=org_a)


def test_specialty_rule_global_and_org_override_coexist(storage: Storage, org_a: int) -> None:
    """Same code can have a global + per-org row (different partial indices)."""
    g = storage.create_specialty_rule(specialty_code="207R00000X")
    o = storage.create_specialty_rule(specialty_code="207R00000X", organization_id=org_a)
    assert g.id != o.id


def test_update_specialty_rule_bumps_version(storage: Storage) -> None:
    r = storage.create_specialty_rule(specialty_code="X", display_name="Old")
    assert r.version_id == 1
    updated = storage.update_specialty_rule(r.id, display_name="New", required_fields={"k": "v"})
    assert updated is not None
    assert updated.display_name == "New"
    assert updated.required_fields == {"k": "v"}
    assert updated.version_id == 2


def test_update_specialty_rule_no_version_bump(storage: Storage) -> None:
    """Typo fixes that shouldn't invalidate rule engine caches."""
    r = storage.create_specialty_rule(specialty_code="X")
    updated = storage.update_specialty_rule(r.id, display_name="corrected", bump_version=False)
    assert updated is not None
    assert updated.version_id == 1


def test_delete_specialty_rule(storage: Storage) -> None:
    r = storage.create_specialty_rule(specialty_code="X")
    assert storage.delete_specialty_rule(r.id) is True
    assert storage.get_specialty_rule(r.id) is None
    assert storage.delete_specialty_rule(r.id) is False


# ============================================================
# PAYER RULES (same pattern)
# ============================================================


def test_create_global_payer_rule(storage: Storage) -> None:
    r = storage.create_payer_rule(
        payer_key="Medicare|medicare",
        display_name="Medicare (Original)",
        referral_required=False,
        auth_required_services={"mri": True},
        auth_typical_turnaround_days=3,
    )
    assert isinstance(r, PayerRule)
    assert r.organization_id is None
    assert r.version_id == 1
    assert r.auth_required_services == {"mri": True}
    assert r.auth_typical_turnaround_days == 3


def test_create_org_payer_rule_override(storage: Storage, org_a: int) -> None:
    r = storage.create_payer_rule(
        payer_key="Kaiser Permanente|hmo",
        organization_id=org_a,
        source="admin_override",
        referral_required=True,
    )
    assert r.organization_id == org_a
    assert r.source == "admin_override"


def test_payer_rule_one_global_per_key(storage: Storage) -> None:
    storage.create_payer_rule(payer_key="K|hmo")
    with pytest.raises(sqlite3.IntegrityError):
        storage.create_payer_rule(payer_key="K|hmo")


def test_payer_rule_one_per_org_per_key(storage: Storage, org_a: int) -> None:
    storage.create_payer_rule(payer_key="K|hmo", organization_id=org_a)
    with pytest.raises(sqlite3.IntegrityError):
        storage.create_payer_rule(payer_key="K|hmo", organization_id=org_a)


def test_list_payer_rules_includes_globals_and_org(storage: Storage, org_a: int) -> None:
    storage.create_payer_rule(payer_key="G|hmo")
    storage.create_payer_rule(payer_key="X|ppo", organization_id=org_a)

    for_a = storage.list_payer_rules(organization_id=org_a)
    assert len(for_a) == 2
    keys = {r.payer_key for r in for_a}
    assert keys == {"G|hmo", "X|ppo"}


def test_update_payer_rule_bumps_version(storage: Storage) -> None:
    r = storage.create_payer_rule(payer_key="K|hmo")
    assert r.version_id == 1
    updated = storage.update_payer_rule(
        r.id, referral_required=True, auth_typical_turnaround_days=5
    )
    assert updated is not None
    assert updated.referral_required is True
    assert updated.auth_typical_turnaround_days == 5
    assert updated.version_id == 2


def test_delete_payer_rule(storage: Storage) -> None:
    r = storage.create_payer_rule(payer_key="K|hmo")
    assert storage.delete_payer_rule(r.id) is True
    assert storage.get_payer_rule(r.id) is None
    assert storage.delete_payer_rule(r.id) is False


# ============================================================
# Cascade + cleanup
# ============================================================


def test_org_delete_cascades_payer_rule_overrides(storage: Storage, org_a: int) -> None:
    r = storage.create_payer_rule(payer_key="K|hmo", organization_id=org_a)
    storage._conn.execute("DELETE FROM organizations WHERE id = ?", (org_a,))
    storage._conn.commit()
    assert storage.get_payer_rule(r.id) is None


def test_org_delete_cascades_specialty_rule_overrides(storage: Storage, org_a: int) -> None:
    r = storage.create_specialty_rule(specialty_code="X", organization_id=org_a)
    storage._conn.execute("DELETE FROM organizations WHERE id = ?", (org_a,))
    storage._conn.commit()
    assert storage.get_specialty_rule(r.id) is None


def test_org_delete_cascades_insurance_plans(storage: Storage, user_a: int, org_a: int) -> None:
    scope = Scope(user_id=user_a, organization_id=org_a, membership_role="owner")
    p = storage.create_insurance_plan(scope, payer_name="Doomed")
    storage._conn.execute("DELETE FROM organizations WHERE id = ?", (org_a,))
    storage._conn.commit()
    row = storage._conn.execute("SELECT * FROM insurance_plans WHERE id = ?", (p.id,)).fetchone()
    assert row is None
