"""Tests for platform-default seed data (Phase 1.G)."""

from __future__ import annotations

from docstats.domain.reference import RULE_SOURCE_VALUES
from docstats.domain.seed import (
    PAYER_DEFAULTS,
    SPECIALTY_DEFAULTS,
    seed_platform_defaults,
)
from docstats.storage import Storage


def test_specialty_defaults_have_12_entries() -> None:
    assert len(SPECIALTY_DEFAULTS) == 12


def test_payer_defaults_have_8_entries() -> None:
    assert len(PAYER_DEFAULTS) == 8


def test_specialty_defaults_well_formed() -> None:
    """Every entry has the keys the seeder expects. Catches drift if the
    model / storage signature changes but the seed dict doesn't."""
    required_keys = {
        "specialty_code",
        "display_name",
        "required_fields",
        "recommended_attachments",
        "intake_questions",
        "urgency_red_flags",
        "common_rejection_reasons",
    }
    for entry in SPECIALTY_DEFAULTS:
        missing = required_keys - entry.keys()
        assert not missing, f"specialty {entry.get('specialty_code')} missing keys: {missing}"
        # JSONB fields must be dicts, not None or str.
        for jsonb_key in (
            "required_fields",
            "recommended_attachments",
            "intake_questions",
            "urgency_red_flags",
            "common_rejection_reasons",
        ):
            assert isinstance(entry[jsonb_key], dict)


def test_specialty_codes_are_unique() -> None:
    codes = [e["specialty_code"] for e in SPECIALTY_DEFAULTS]
    assert len(codes) == len(set(codes)), "duplicate specialty_code in SPECIALTY_DEFAULTS"


def test_payer_defaults_well_formed() -> None:
    required_keys = {
        "payer_key",
        "display_name",
        "referral_required",
        "auth_required_services",
        "auth_typical_turnaround_days",
        "records_required",
        "notes",
    }
    for entry in PAYER_DEFAULTS:
        missing = required_keys - entry.keys()
        assert not missing, f"payer {entry.get('payer_key')} missing keys: {missing}"
        assert isinstance(entry["referral_required"], bool)
        assert isinstance(entry["auth_required_services"], dict)
        assert isinstance(entry["records_required"], dict)


def test_payer_keys_are_unique() -> None:
    keys = [e["payer_key"] for e in PAYER_DEFAULTS]
    assert len(keys) == len(set(keys)), "duplicate payer_key in PAYER_DEFAULTS"


def test_seed_platform_defaults_creates_all(storage: Storage) -> None:
    result = seed_platform_defaults(storage)
    assert result["specialty_rules_created"] == 12
    assert result["payer_rules_created"] == 8
    assert result["specialty_rules_skipped"] == 0
    assert result["payer_rules_skipped"] == 0

    # Rows landed as global (organization_id IS NULL).
    specialty_rows = storage.list_specialty_rules(organization_id=None)
    assert len(specialty_rows) == 12
    assert all(r.organization_id is None for r in specialty_rows)
    assert all(r.source == "seed" for r in specialty_rows)

    payer_rows = storage.list_payer_rules(organization_id=None)
    assert len(payer_rows) == 8
    assert all(r.organization_id is None for r in payer_rows)
    assert all(r.source == "seed" for r in payer_rows)


def test_seed_platform_defaults_is_idempotent(storage: Storage) -> None:
    """Running the seeder twice must not produce duplicates or errors."""
    seed_platform_defaults(storage)
    result = seed_platform_defaults(storage)
    assert result["specialty_rules_created"] == 0
    assert result["specialty_rules_skipped"] == 12
    assert result["payer_rules_created"] == 0
    assert result["payer_rules_skipped"] == 8

    # Still 12 + 8, no duplicates.
    assert len(storage.list_specialty_rules(organization_id=None)) == 12
    assert len(storage.list_payer_rules(organization_id=None)) == 8


def test_seed_platform_defaults_overwrite(storage: Storage) -> None:
    """overwrite=True restores seed values without bumping version_id.

    The seeder uses ``bump_version=False`` on the overwrite path: a canonical
    restore shouldn't invalidate the rule-engine cache for a rule whose
    content is about to become canonical again. Version only changes via
    real admin edits.
    """
    seed_platform_defaults(storage)
    # First the user edits a row as admin_override (which DOES bump version).
    cardio = next(
        r
        for r in storage.list_specialty_rules(organization_id=None)
        if r.specialty_code == "207RC0000X"
    )
    storage.update_specialty_rule(
        cardio.id,
        display_name="Cardiology (edited)",
        source="admin_override",
    )
    before_version = storage.get_specialty_rule(cardio.id).version_id  # type: ignore[union-attr]

    result = seed_platform_defaults(storage, overwrite=True)
    assert result["specialty_rules_overwritten"] == 12

    after = storage.get_specialty_rule(cardio.id)
    assert after is not None
    assert after.display_name == "Cardiology"  # seed value wins
    assert after.source == "seed"
    # version_id must NOT bump on a canonical restore — caches stay valid.
    assert after.version_id == before_version


def test_seed_overwrite_can_reset_payer_field_to_none(storage: Storage) -> None:
    """Seed overwrite writes every field literally, so an admin who set
    Medicare's ``auth_typical_turnaround_days`` to a concrete integer gets
    it reset back to None (the seed value) on re-run. Regression guard for
    the None-means-skip gap in the default update path.
    """
    seed_platform_defaults(storage)
    medicare = next(
        r
        for r in storage.list_payer_rules(organization_id=None)
        if r.payer_key == "Medicare|medicare"
    )
    # Admin fills in a value the seed intentionally leaves blank.
    storage.update_payer_rule(medicare.id, auth_typical_turnaround_days=5)
    assert storage.get_payer_rule(medicare.id).auth_typical_turnaround_days == 5  # type: ignore[union-attr]

    seed_platform_defaults(storage, overwrite=True)
    after = storage.get_payer_rule(medicare.id)
    assert after is not None
    # Seed has auth_typical_turnaround_days=None for Medicare; overwrite=True
    # must actually write that None back.
    assert after.auth_typical_turnaround_days is None


def test_all_seeded_sources_are_valid(storage: Storage) -> None:
    """Every seeded row uses a valid RULE_SOURCE_VALUES entry."""
    seed_platform_defaults(storage)
    for r in storage.list_specialty_rules(organization_id=None):
        assert r.source in RULE_SOURCE_VALUES
    for r in storage.list_payer_rules(organization_id=None):
        assert r.source in RULE_SOURCE_VALUES


def test_seed_doesnt_overwrite_admin_overrides_by_default(storage: Storage) -> None:
    """The idempotent default path must NOT trample admin edits — that's
    the whole reason we version rules and track source."""
    seed_platform_defaults(storage)
    cardio = next(
        r
        for r in storage.list_specialty_rules(organization_id=None)
        if r.specialty_code == "207RC0000X"
    )
    storage.update_specialty_rule(
        cardio.id,
        display_name="Cardiology (locally customized)",
        source="admin_override",
    )

    # Re-run without overwrite — admin's customization must survive.
    result = seed_platform_defaults(storage)
    assert result["specialty_rules_skipped"] == 12
    after = storage.get_specialty_rule(cardio.id)
    assert after is not None
    assert after.display_name == "Cardiology (locally customized)"
    assert after.source == "admin_override"
