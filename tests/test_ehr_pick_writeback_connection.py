"""Tests for the EHR write-back connection resolver (Issue #155)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from docstats.ehr import pick_writeback_connection
from docstats.scope import Scope


def _seed_user_with_dependent(storage, *, fhir_id: str = "EPIC-CHILD"):
    user_id = storage.create_user("parent@example.com", "pw")
    patient = storage.create_patient(
        Scope(user_id=user_id),
        first_name="Kid",
        last_name="Doe",
        relationship="child",
        ehr_fhir_id=fhir_id,
    )
    return user_id, patient


def _now_plus(hours: int):
    return datetime.now(tz=timezone.utc) + timedelta(hours=hours)


def test_resolver_returns_none_without_ehr_fhir_id(storage):
    user_id = storage.create_user("u@example.com", "pw")
    patient = storage.create_patient(
        Scope(user_id=user_id),
        first_name="Self",
        last_name="Doe",
        relationship=None,
    )
    assert patient.ehr_fhir_id is None
    assert pick_writeback_connection(storage, Scope(user_id=user_id), patient) is None


def test_resolver_prefers_patient_scoped_over_user_scoped(storage):
    """When both connections claim the same patient_fhir_id, patient-scoped wins."""
    user_id, patient = _seed_user_with_dependent(storage)

    # User-scoped connection: parent's own MyChart that happens to share the
    # child's FHIR id (unlikely in real life — exercises strict precedence).
    storage.create_ehr_connection(
        user_id=user_id,
        ehr_vendor="epic_sandbox",
        iss="https://epic.test/r4",
        access_token_enc="USR_AT",
        refresh_token_enc="USR_RT",
        expires_at=_now_plus(1),
        scope="openid",
        patient_fhir_id="EPIC-CHILD",
    )
    # Patient-scoped connection (the dependent's own MyChart proxy).
    child_conn = storage.create_patient_ehr_connection(
        patient_id=patient.id,
        ehr_vendor="epic_sandbox",
        iss="https://epic.test/r4",
        access_token_enc="CHILD_AT",
        refresh_token_enc="CHILD_RT",
        expires_at=_now_plus(1),
        scope="openid",
        patient_fhir_id="EPIC-CHILD",
    )

    picked = pick_writeback_connection(storage, Scope(user_id=user_id), patient)
    assert picked is not None and picked.id == child_conn.id


def test_resolver_falls_back_to_user_scoped_when_no_patient_conn(storage):
    user_id, patient = _seed_user_with_dependent(storage, fhir_id="EPIC-SELF")
    user_conn = storage.create_ehr_connection(
        user_id=user_id,
        ehr_vendor="epic_sandbox",
        iss="https://epic.test/r4",
        access_token_enc="USR_AT",
        refresh_token_enc="USR_RT",
        expires_at=_now_plus(1),
        scope="openid",
        patient_fhir_id="EPIC-SELF",
    )
    picked = pick_writeback_connection(storage, Scope(user_id=user_id), patient)
    assert picked is not None and picked.id == user_conn.id


def test_resolver_ignores_connections_with_mismatched_fhir_id(storage):
    """Multi-vendor mis-routing guard from PR #142 stays intact across all
    three candidate sets — an Epic connection that imported a *different*
    patient must not satisfy a write-back for this patient."""
    user_id, patient = _seed_user_with_dependent(storage, fhir_id="EPIC-A")
    # User connection points at a different patient.
    storage.create_ehr_connection(
        user_id=user_id,
        ehr_vendor="epic_sandbox",
        iss="https://epic.test/r4",
        access_token_enc="USR_AT",
        refresh_token_enc="USR_RT",
        expires_at=_now_plus(1),
        scope="openid",
        patient_fhir_id="EPIC-OTHER",
    )
    assert pick_writeback_connection(storage, Scope(user_id=user_id), patient) is None
