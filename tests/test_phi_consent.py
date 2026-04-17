"""Tests for PHI-consent tracking (Phase 0.D)."""

from __future__ import annotations

import pytest

from docstats.phi import (
    CURRENT_PHI_CONSENT_VERSION,
    PhiConsentRequiredException,
    _has_current_phi_consent,
    require_phi_consent,
)
from docstats.storage import Storage


@pytest.fixture
def user_id(storage: Storage) -> int:
    return storage.create_user("phi@example.com", "hashed")


# --- Storage ---


def test_record_phi_consent_populates_four_columns(storage: Storage, user_id: int) -> None:
    storage.record_phi_consent(
        user_id,
        phi_consent_version="1.0",
        ip_address="203.0.113.7",
        user_agent="pytest",
    )
    user = storage.get_user_by_id(user_id)
    assert user is not None
    assert user["phi_consent_version"] == "1.0"
    assert user["phi_consent_at"] is not None
    assert user["phi_consent_ip"] == "203.0.113.7"
    assert user["phi_consent_user_agent"] == "pytest"


def test_phi_consent_independent_of_terms_acceptance(storage: Storage, user_id: int) -> None:
    """Accepting the general ToS must NOT imply PHI consent (and vice versa).

    These are versioned independently so a ToS update doesn't invalidate PHI
    consent and a PHI-scope bump doesn't force full ToS re-acceptance.
    """
    storage.record_terms_acceptance(
        user_id,
        terms_version="1.0",
        ip_address="1.1.1.1",
        user_agent="ua",
    )
    user = storage.get_user_by_id(user_id)
    assert user is not None
    assert user["terms_accepted_at"] is not None
    assert user.get("phi_consent_at") is None

    storage.record_phi_consent(
        user_id,
        phi_consent_version="1.0",
        ip_address="2.2.2.2",
        user_agent="ua2",
    )
    user = storage.get_user_by_id(user_id)
    assert user is not None
    assert user["terms_accepted_at"] is not None
    assert user["phi_consent_at"] is not None
    assert user["phi_consent_ip"] == "2.2.2.2"
    assert user["terms_ip"] == "1.1.1.1"  # unchanged


def test_record_phi_consent_is_idempotent_for_same_version(storage: Storage, user_id: int) -> None:
    """Re-accepting the same version overwrites timestamp + IP + UA — useful
    when the user re-confirms consent from a new device, for example."""
    storage.record_phi_consent(
        user_id, phi_consent_version="1.0", ip_address="1.1.1.1", user_agent="old"
    )
    storage.record_phi_consent(
        user_id, phi_consent_version="1.0", ip_address="2.2.2.2", user_agent="new"
    )
    user = storage.get_user_by_id(user_id)
    assert user is not None
    assert user["phi_consent_ip"] == "2.2.2.2"
    assert user["phi_consent_user_agent"] == "new"


# --- Helper + dependency ---


def test_has_current_phi_consent_false_when_missing() -> None:
    assert _has_current_phi_consent({"id": 1}) is False


def test_has_current_phi_consent_false_on_version_mismatch() -> None:
    user = {
        "id": 1,
        "phi_consent_at": "2026-04-17T00:00:00+00:00",
        "phi_consent_version": "0.9",  # older than current
    }
    assert _has_current_phi_consent(user) is False


def test_has_current_phi_consent_true_on_current_version() -> None:
    user = {
        "id": 1,
        "phi_consent_at": "2026-04-17T00:00:00+00:00",
        "phi_consent_version": CURRENT_PHI_CONSENT_VERSION,
    }
    assert _has_current_phi_consent(user) is True


def test_require_phi_consent_raises_without_consent() -> None:
    user = {"id": 1, "email": "a@b"}
    with pytest.raises(PhiConsentRequiredException):
        require_phi_consent(user=user)


def test_require_phi_consent_returns_user_with_consent() -> None:
    user = {
        "id": 1,
        "email": "a@b",
        "phi_consent_at": "2026-04-17T00:00:00+00:00",
        "phi_consent_version": CURRENT_PHI_CONSENT_VERSION,
    }
    assert require_phi_consent(user=user) is user


def test_phi_consent_exception_extends_auth_required() -> None:
    """So the existing HX-Redirect / 303 handler catches it without a new
    exception handler. Phase 2 can swap in a dedicated consent-prompt handler."""
    from docstats.auth import AuthRequiredException

    assert issubclass(PhiConsentRequiredException, AuthRequiredException)


def test_record_phi_consent_truncates_oversized_ip_and_user_agent(
    storage: Storage, user_id: int
) -> None:
    """Cap hostile / oversized headers at the storage boundary so the users
    row can't be blown up by an uncapped caller — mirrors the 500-char cap
    that domain/audit.record() applies."""
    from docstats.validators import IP_MAX_LENGTH, USER_AGENT_MAX_LENGTH

    storage.record_phi_consent(
        user_id,
        phi_consent_version="1.0",
        ip_address="x" * 200,
        user_agent="y" * 2000,
    )
    user = storage.get_user_by_id(user_id)
    assert user is not None
    assert user["phi_consent_ip"] is not None
    assert len(user["phi_consent_ip"]) == IP_MAX_LENGTH
    assert user["phi_consent_user_agent"] is not None
    assert len(user["phi_consent_user_agent"]) == USER_AGENT_MAX_LENGTH
