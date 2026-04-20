"""Tests for the specialty + payer rules engine (Phase 3.A)."""

from __future__ import annotations

from pathlib import Path

import pytest

from docstats.domain.rules import (
    ResolvedRuleSet,
    detect_red_flags,
    evaluate,
    resolve_payer_rule,
    resolve_ruleset,
    resolve_specialty_rule,
)
from docstats.domain.seed import seed_platform_defaults
from docstats.scope import Scope
from docstats.storage import Storage


@pytest.fixture
def seeded_storage(tmp_path: Path) -> Storage:
    """Storage with the 12 specialty + 8 payer platform defaults seeded."""
    s = Storage(db_path=tmp_path / "test.db")
    seed_platform_defaults(s)
    return s


@pytest.fixture
def user_id(seeded_storage: Storage) -> int:
    return seeded_storage.create_user("a@example.com", "hashed")


def _make_referral(
    storage: Storage,
    user_id: int,
    *,
    reason: str = "Chest pain evaluation",
    clinical_question: str | None = None,
    specialty_code: str | None = None,
    specialty_desc: str | None = None,
    receiving_organization_name: str | None = "Heart Clinic",
    payer_plan_id: int | None = None,
):
    scope = Scope(user_id=user_id)
    patient = storage.create_patient(
        scope, first_name="Jane", last_name="Doe", created_by_user_id=user_id
    )
    return storage.create_referral(
        scope,
        patient_id=patient.id,
        reason=reason,
        clinical_question=clinical_question,
        specialty_code=specialty_code,
        specialty_desc=specialty_desc,
        receiving_organization_name=receiving_organization_name,
        payer_plan_id=payer_plan_id,
        created_by_user_id=user_id,
    )


# --- resolve_specialty_rule ---


def test_resolve_unknown_code(seeded_storage: Storage) -> None:
    assert resolve_specialty_rule(seeded_storage, None, "UNKNOWN_CODE") is None


def test_resolve_none_code(seeded_storage: Storage) -> None:
    assert resolve_specialty_rule(seeded_storage, None, None) is None


def test_resolve_platform_default(seeded_storage: Storage) -> None:
    rule = resolve_specialty_rule(seeded_storage, None, "207RC0000X")  # Cardiology
    assert rule is not None
    assert rule.display_name == "Cardiology"
    assert rule.organization_id is None


def test_resolve_org_override_wins(seeded_storage: Storage, user_id: int) -> None:
    org = seeded_storage.create_organization(name="Org A", slug="org-a")
    seeded_storage.create_membership(organization_id=org.id, user_id=user_id, role="owner")
    # Override the cardiology rule with a tightened required-fields set.
    override = seeded_storage.create_specialty_rule(
        specialty_code="207RC0000X",
        organization_id=org.id,
        display_name="Cardiology (Org A)",
        required_fields={"fields": ["reason", "urgency"]},
        source="admin_override",
    )
    resolved = resolve_specialty_rule(seeded_storage, org.id, "207RC0000X")
    assert resolved is not None
    assert resolved.id == override.id
    assert resolved.display_name == "Cardiology (Org A)"


# --- detect_red_flags ---


def test_red_flags_case_insensitive(seeded_storage: Storage) -> None:
    cardio = resolve_specialty_rule(seeded_storage, None, "207RC0000X")
    assert cardio is not None

    class _R:
        reason = "Patient with CHEST PAIN and dyspnea at rest for 2 days"
        clinical_question = None

    hits = detect_red_flags(_R(), cardio)
    assert "chest pain" in hits
    assert "dyspnea at rest" in hits


def test_red_flags_no_specialty_returns_empty() -> None:
    class _R:
        reason = "chest pain"
        clinical_question = None

    assert detect_red_flags(_R(), None) == []


def test_red_flags_blank_text() -> None:
    # Defensive: a specialty rule with no reason/question can't match anything
    class _R:
        reason = None
        clinical_question = "   "

    class _Rule:
        urgency_red_flags = {"keywords": ["chest pain"]}

    assert detect_red_flags(_R(), _Rule()) == []


# --- evaluate ---


def test_evaluate_no_rules_uses_baseline(seeded_storage: Storage, user_id: int) -> None:
    """With no specialty/payer match, evaluate returns baseline items only."""
    r = _make_referral(seeded_storage, user_id, specialty_code=None)
    report = evaluate(r, ResolvedRuleSet(specialty=None, payer=None))
    assert report.red_flags == []
    assert report.recommended_attachments == []
    assert report.specialty_display_name is None
    # Baseline items present
    codes = {i.code for i in report.items}
    assert "reason" in codes
    assert "receiving_side" in codes
    assert "specialty" in codes


def test_evaluate_cardiology_adds_diagnosis_required(seeded_storage: Storage, user_id: int) -> None:
    """Cardiology's required_fields lists ['reason', 'clinical_question',
    'diagnosis_primary_icd']. The third is NOT a baseline item — engine
    adds it."""
    r = _make_referral(
        seeded_storage,
        user_id,
        specialty_code="207RC0000X",
        specialty_desc="Cardiology",
    )
    ruleset = resolve_ruleset(seeded_storage, Scope(user_id=user_id), r)
    report = evaluate(r, ruleset)
    # Baseline items don't include primary_diagnosis as required — that's
    # recommended-only in baseline. Specialty promotion makes it required.
    diag_items = [i for i in report.items if "diagnosis" in i.code]
    assert any(i.required and not i.satisfied for i in diag_items)
    assert report.specialty_display_name == "Cardiology"


def test_evaluate_icd_requirement_not_satisfied_by_free_text_only(
    seeded_storage: Storage, user_id: int
) -> None:
    """Regression: specialty requiring ``diagnosis_primary_icd`` must NOT be
    marked satisfied when only ``diagnosis_primary_text`` is present.

    Baseline ``primary_diagnosis`` is satisfied by EITHER the ICD code OR
    the free-text field. But when a specialty rule names
    ``diagnosis_primary_icd`` specifically in ``required_fields``, the
    promoted baseline item must tighten its satisfaction check to the ICD
    column alone — otherwise an admin enforcing "ICD code required" gets a
    false-complete result whenever coordinators fill only the text field.
    """
    # Cardiology's seeded rule includes diagnosis_primary_icd as required.
    scope = Scope(user_id=user_id)
    patient = seeded_storage.create_patient(
        scope, first_name="Jane", last_name="Doe", created_by_user_id=user_id
    )
    r = seeded_storage.create_referral(
        scope,
        patient_id=patient.id,
        reason="Chest pain",
        clinical_question="Needs cards eval",
        specialty_code="207RC0000X",
        specialty_desc="Cardiology",
        receiving_organization_name="Heart Clinic",
        created_by_user_id=user_id,
    )
    # Populate ONLY the free-text diagnosis field — no ICD code.
    seeded_storage.add_referral_diagnosis(
        scope,
        r.id,
        icd10_code="",
        icd10_desc="Angina (no code yet)",
        is_primary=True,
        source="user_entered",
    )
    r = seeded_storage.get_referral(scope, r.id)
    assert r is not None
    # Precondition: free text present, ICD code empty.
    assert r.diagnosis_primary_text and r.diagnosis_primary_text.strip()
    assert not r.diagnosis_primary_icd

    ruleset = resolve_ruleset(seeded_storage, scope, r)
    report = evaluate(r, ruleset)
    diag = next(i for i in report.items if i.code == "primary_diagnosis")
    assert diag.required is True
    # The bug before this fix: satisfied=True because baseline is permissive.
    assert diag.satisfied is False, (
        "Specialty-required ICD code should NOT be marked satisfied by free-text diagnosis alone."
    )


def test_evaluate_icd_requirement_satisfied_when_icd_present(
    seeded_storage: Storage, user_id: int
) -> None:
    """Companion to the regression above: when the ICD code IS present, the
    promoted baseline item is satisfied. Both free-text-only and
    code-only-or-both should not mark the same item satisfied when the
    specialty tightened the check."""
    scope = Scope(user_id=user_id)
    patient = seeded_storage.create_patient(
        scope, first_name="Jane", last_name="Doe", created_by_user_id=user_id
    )
    r = seeded_storage.create_referral(
        scope,
        patient_id=patient.id,
        reason="Chest pain",
        clinical_question="Needs cards eval",
        specialty_code="207RC0000X",
        specialty_desc="Cardiology",
        receiving_organization_name="Heart Clinic",
        created_by_user_id=user_id,
    )
    seeded_storage.add_referral_diagnosis(
        scope,
        r.id,
        icd10_code="I20.9",
        icd10_desc="Angina pectoris, unspecified",
        is_primary=True,
        source="user_entered",
    )
    r = seeded_storage.get_referral(scope, r.id)
    assert r is not None
    ruleset = resolve_ruleset(seeded_storage, scope, r)
    report = evaluate(r, ruleset)
    diag = next(i for i in report.items if i.code == "primary_diagnosis")
    assert diag.required is True
    assert diag.satisfied is True


def test_evaluate_red_flags_populate(seeded_storage: Storage, user_id: int) -> None:
    r = _make_referral(
        seeded_storage,
        user_id,
        reason="70yo with acute chest pain and syncope",
        specialty_code="207RC0000X",
    )
    ruleset = resolve_ruleset(seeded_storage, Scope(user_id=user_id), r)
    report = evaluate(r, ruleset)
    assert "chest pain" in report.red_flags
    assert "syncope" in report.red_flags


def test_evaluate_recommended_attachments(seeded_storage: Storage, user_id: int) -> None:
    r = _make_referral(
        seeded_storage,
        user_id,
        specialty_code="207RC0000X",
    )
    ruleset = resolve_ruleset(seeded_storage, Scope(user_id=user_id), r)
    report = evaluate(r, ruleset)
    assert any("EKG" in label for label in report.recommended_attachments)


def test_evaluate_rejection_hints_from_specialty(seeded_storage: Storage, user_id: int) -> None:
    r = _make_referral(
        seeded_storage,
        user_id,
        specialty_code="207RC0000X",
    )
    ruleset = resolve_ruleset(seeded_storage, Scope(user_id=user_id), r)
    report = evaluate(r, ruleset)
    assert any("EKG" in hint for hint in report.rejection_hints)


def test_evaluate_payer_referral_required_hint(seeded_storage: Storage, user_id: int) -> None:
    """Kaiser HMO has referral_required=True. Referral without an auth number
    should surface a hint."""
    scope = Scope(user_id=user_id)
    plan = seeded_storage.create_insurance_plan(
        scope,
        payer_name="Kaiser Permanente",
        plan_type="hmo",
    )
    r = _make_referral(seeded_storage, user_id, payer_plan_id=plan.id)
    ruleset = resolve_ruleset(seeded_storage, scope, r)
    assert ruleset.payer is not None
    report = evaluate(r, ruleset)
    assert any(
        "authorization" in hint.lower() or "referral" in hint.lower()
        for hint in report.rejection_hints
    )


def test_evaluate_payer_no_hint_when_auth_present(seeded_storage: Storage, user_id: int) -> None:
    """Auth number already present → no "referral required" hint."""
    scope = Scope(user_id=user_id)
    plan = seeded_storage.create_insurance_plan(
        scope,
        payer_name="Kaiser Permanente",
        plan_type="hmo",
    )
    r = _make_referral(seeded_storage, user_id, payer_plan_id=plan.id)
    seeded_storage.update_referral(scope, r.id, authorization_number="AUTH-123")
    r_updated = seeded_storage.get_referral(scope, r.id)
    assert r_updated is not None
    ruleset = resolve_ruleset(seeded_storage, scope, r_updated)
    report = evaluate(r_updated, ruleset)
    assert not any("typically requires a referral" in hint for hint in report.rejection_hints)


# --- resolve_payer_rule ---


def test_resolve_payer_rule_from_plan(seeded_storage: Storage, user_id: int) -> None:
    scope = Scope(user_id=user_id)
    plan = seeded_storage.create_insurance_plan(scope, payer_name="Aetna", plan_type="hmo")
    rule = resolve_payer_rule(seeded_storage, None, plan)
    assert rule is not None
    assert rule.payer_key == "Aetna|hmo"


def test_resolve_payer_rule_unknown_plan_returns_none(
    seeded_storage: Storage, user_id: int
) -> None:
    scope = Scope(user_id=user_id)
    plan = seeded_storage.create_insurance_plan(scope, payer_name="Acme Health", plan_type="hmo")
    assert resolve_payer_rule(seeded_storage, None, plan) is None


def test_resolve_payer_rule_none_plan() -> None:
    assert resolve_payer_rule(None, None, None) is None  # type: ignore[arg-type]


def test_create_insurance_plan_rejects_pipe_in_payer_name(
    seeded_storage: Storage, user_id: int
) -> None:
    """payer_key is derived as ``{payer_name}|{plan_type}``; reject ``|`` in
    payer_name at the storage boundary so the derived key stays unambiguous."""
    scope = Scope(user_id=user_id)
    with pytest.raises(ValueError, match=r"'\|'"):
        seeded_storage.create_insurance_plan(
            scope,
            payer_name="Evil|Injected",
            plan_type="ppo",
        )


# --- Boot-time seeding (Phase 3.D) ---


def test_lifespan_calls_seed_platform_defaults(monkeypatch):
    """web.py's lifespan context calls seed_platform_defaults. We can't
    route the call through a dep override (startup runs before request DI),
    so we patch the module-level import and assert the hook fired.

    conftest sets ``DOCSTATS_SKIP_BOOT_SEED=1`` to prevent accidental
    real-DB mutation; unset it for this test so the lifespan actually runs
    the seed path.
    """
    from fastapi.testclient import TestClient

    from docstats import web as web_module

    monkeypatch.delenv("DOCSTATS_SKIP_BOOT_SEED", raising=False)

    called: dict[str, int] = {"count": 0}

    def _fake_seed(_storage, *, overwrite: bool = False) -> dict[str, int]:
        called["count"] += 1
        return {
            "specialty_rules_created": 0,
            "specialty_rules_skipped": 0,
            "specialty_rules_overwritten": 0,
            "payer_rules_created": 0,
            "payer_rules_skipped": 0,
            "payer_rules_overwritten": 0,
        }

    monkeypatch.setattr(web_module, "seed_platform_defaults", _fake_seed)
    with TestClient(web_module.app):
        pass  # enter + exit lifespan
    assert called["count"] == 1


def test_lifespan_seed_failure_is_non_fatal(monkeypatch, caplog):
    """A Supabase blip at boot shouldn't knock the app down."""
    from fastapi.testclient import TestClient

    from docstats import web as web_module

    monkeypatch.delenv("DOCSTATS_SKIP_BOOT_SEED", raising=False)

    def _boom(_storage, *, overwrite: bool = False):
        raise RuntimeError("supabase 500")

    monkeypatch.setattr(web_module, "seed_platform_defaults", _boom)
    with caplog.at_level("ERROR"):
        with TestClient(web_module.app):
            pass
    assert any("seed_platform_defaults failed" in rec.message for rec in caplog.records)


def test_lifespan_skip_env_var(monkeypatch):
    """DOCSTATS_SKIP_BOOT_SEED=1 bypasses the seed call entirely."""
    from fastapi.testclient import TestClient

    from docstats import web as web_module

    monkeypatch.setenv("DOCSTATS_SKIP_BOOT_SEED", "1")

    called: dict[str, int] = {"count": 0}

    def _fake_seed(_storage, *, overwrite: bool = False) -> dict[str, int]:
        called["count"] += 1
        return {}

    monkeypatch.setattr(web_module, "seed_platform_defaults", _fake_seed)
    with TestClient(web_module.app):
        pass
    assert called["count"] == 0


def test_cli_seed_rules_command(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from docstats.cli import app as cli_app
    from docstats.storage import Storage

    storage = Storage(db_path=tmp_path / "cli.db")
    monkeypatch.setattr("docstats.cli._get_storage", lambda: storage)

    result = CliRunner().invoke(cli_app, ["seed-rules"])
    assert result.exit_code == 0
    assert "specialty_rules_created: 12" in result.stdout
    assert "payer_rules_created: 8" in result.stdout

    # Second run: idempotent, all skipped.
    result2 = CliRunner().invoke(cli_app, ["seed-rules"])
    assert result2.exit_code == 0
    assert "specialty_rules_skipped: 12" in result2.stdout
