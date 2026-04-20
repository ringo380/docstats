"""Phase 5.A — Referral export (PDF) tests.

Covers:

- Pure-function unit test on ``render_referral_summary`` (PDF header, byte
  length sanity, clinical sub-entities rendered into the document text).
- Route-level tests on ``GET /referrals/{id}/export.pdf``: happy path, scope
  isolation, unknown artifact, PHI-consent gate, audit + event emission.
- Skipped cleanly if WeasyPrint's system libs aren't importable.

WeasyPrint is installed in the ``[web]`` extra. If the underlying Pango /
Cairo shared libraries aren't on the host, import fails — the tests skip at
collection time rather than red-flag the full suite.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("weasyprint", reason="WeasyPrint system libs not installed")

from docstats.auth import get_current_user  # noqa: E402
from docstats.domain.patients import Patient  # noqa: E402
from docstats.domain.referrals import CompletenessItem, Referral  # noqa: E402
from docstats.domain.rules import CompletenessReportV2  # noqa: E402
from docstats.exports import (  # noqa: E402
    render_attachments_checklist,
    render_missing_info,
    render_patient_summary,
    render_referral_summary,
    render_scheduling_summary,
)
from docstats.phi import CURRENT_PHI_CONSENT_VERSION  # noqa: E402
from docstats.scope import Scope  # noqa: E402
from docstats.storage import Storage, get_storage  # noqa: E402
from docstats.web import app  # noqa: E402


def _fake_user(user_id: int, email: str = "a@example.com", *, consent: bool = True):
    return {
        "id": user_id,
        "email": email,
        "display_name": None,
        "first_name": "Coordinator",
        "last_name": "Tester",
        "github_id": None,
        "github_login": None,
        "password_hash": "hashed_pw",
        "created_at": "2026-01-01",
        "last_login_at": None,
        "terms_accepted_at": "2026-01-01",
        "phi_consent_at": "2026-01-01" if consent else None,
        "phi_consent_version": CURRENT_PHI_CONSENT_VERSION if consent else None,
        "phi_consent_ip": None,
        "phi_consent_user_agent": None,
        "active_org_id": None,
    }


# ---------- Unit: render_referral_summary ----------


def _fixture_patient_referral():
    now = datetime(2026, 4, 19, 12, 0, tzinfo=timezone.utc)
    patient = Patient(
        id=1,
        scope_user_id=1,
        first_name="Jane",
        last_name="Doe",
        middle_name="M",
        date_of_birth="1980-05-15",
        sex="F",
        mrn="MRN-1234",
        phone="4155551234",
        created_at=now,
        updated_at=now,
    )
    referral = Referral(
        id=42,
        scope_user_id=1,
        patient_id=1,
        reason="Chest pain eval intermittent",
        clinical_question="R/o cardiac etiology",
        specialty_code="207RC0000X",
        specialty_desc="Cardiovascular Disease",
        receiving_organization_name="Heart Care Associates",
        receiving_provider_npi="1234567890",
        referring_provider_name="Dr. A. Sender",
        referring_organization="Primary Care HMO",
        referring_provider_npi="0987654321",
        diagnosis_primary_icd="R07.9",
        diagnosis_primary_text="Chest pain unspecified",
        urgency="urgent",
        status="ready",
        authorization_status="required_pending",
        authorization_number="AUTH-55555",
        created_at=now,
        updated_at=now,
    )
    return patient, referral, now


def test_render_summary_returns_pdf_bytes():
    patient, referral, now = _fixture_patient_referral()
    pdf = render_referral_summary(
        referral=referral,
        patient=patient,
        generated_at=now,
        generated_by_label="Coordinator Tester",
    )
    assert pdf.startswith(b"%PDF-")
    assert pdf.endswith(b"%%EOF\n") or pdf.rstrip().endswith(b"%%EOF")
    # Sanity: a one-page clinical summary should be at least a few kB.
    assert len(pdf) > 2000


def test_render_summary_respects_none_dob():
    _, referral, now = _fixture_patient_referral()
    now = datetime(2026, 4, 19, 12, 0, tzinfo=timezone.utc)
    patient = Patient(
        id=1,
        scope_user_id=1,
        first_name="John",
        last_name="Roe",
        date_of_birth=None,
        created_at=now,
        updated_at=now,
    )
    pdf = render_referral_summary(referral=referral, patient=patient, generated_at=now)
    assert pdf.startswith(b"%PDF-")


# ---------- Route: happy path + audit ----------


@pytest.fixture
def solo_client(tmp_path: Path):
    storage = Storage(db_path=tmp_path / "test.db")
    user_id = storage.create_user("a@example.com", "hashed_pw")
    storage.update_user_profile(
        user_id,
        first_name="Coordinator",
        last_name="Tester",
        display_name="Coordinator Tester",
    )
    storage.record_phi_consent(
        user_id=user_id,
        phi_consent_version=CURRENT_PHI_CONSENT_VERSION,
        ip_address="",
        user_agent="",
    )
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_current_user] = lambda: _fake_user(user_id)
    yield TestClient(app), storage, user_id
    app.dependency_overrides.clear()


def _seed_referral(storage: Storage, user_id: int):
    scope = Scope(user_id=user_id)
    patient = storage.create_patient(
        scope,
        first_name="Jane",
        last_name="Doe",
        date_of_birth="1980-05-15",
        created_by_user_id=user_id,
    )
    referral = storage.create_referral(
        scope,
        patient_id=patient.id,
        reason="Chest pain eval",
        urgency="urgent",
        specialty_desc="Cardiology",
        receiving_organization_name="Heart Clinic",
        created_by_user_id=user_id,
    )
    return patient, referral


def test_export_pdf_happy_path(solo_client):
    client, storage, user_id = solo_client
    _, referral = _seed_referral(storage, user_id)

    resp = client.get(f"/referrals/{referral.id}/export.pdf")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.content.startswith(b"%PDF-")
    assert "inline" in resp.headers["content-disposition"]
    assert f"referral-{referral.id}-summary.pdf" in resp.headers["content-disposition"]
    # Lock the exact Cache-Control value so future edits don't silently drift
    # from the CLAUDE.md / AGENTS.md documented contract.
    assert resp.headers.get("cache-control") == "private, no-store"
    assert resp.headers.get("x-content-type-options") == "nosniff"


def test_export_records_audit_and_event(solo_client):
    client, storage, user_id = solo_client
    _, referral = _seed_referral(storage, user_id)

    resp = client.get(f"/referrals/{referral.id}/export.pdf")
    assert resp.status_code == 200

    events = storage.list_referral_events(Scope(user_id=user_id), referral.id, limit=20)
    assert any(e.event_type == "exported" for e in events), [e.event_type for e in events]

    audit_rows = storage.list_audit_events(limit=20)
    assert any(
        a.action == "referral.export" and a.entity_id == str(referral.id) for a in audit_rows
    )


def test_export_unknown_artifact_400(solo_client):
    client, storage, user_id = solo_client
    _, referral = _seed_referral(storage, user_id)
    resp = client.get(f"/referrals/{referral.id}/export.pdf", params={"artifact": "bogus"})
    assert resp.status_code == 400


def test_export_oversized_artifact_param_422(solo_client):
    """``max_length=32`` on the Query param caps oversized input at the boundary."""
    client, storage, user_id = solo_client
    _, referral = _seed_referral(storage, user_id)
    resp = client.get(f"/referrals/{referral.id}/export.pdf", params={"artifact": "x" * 64})
    assert resp.status_code == 422


def test_export_with_clinical_sub_entities(solo_client):
    """Seed diagnoses/meds/allergies/attachments so the {% if %} branches fire.

    The route smoke-tests end-to-end wiring; the byte-size assertion that
    actually proves the {% if %} blocks emitted content lives in
    ``test_render_summary_with_sub_entities_is_larger`` where timestamps are
    pinned deterministically.
    """
    client, storage, user_id = solo_client
    _, referral = _seed_referral(storage, user_id)
    scope = Scope(user_id=user_id)
    storage.add_referral_diagnosis(
        scope, referral.id, icd10_code="R07.9", icd10_desc="Chest pain", is_primary=True
    )
    storage.add_referral_medication(
        scope, referral.id, name="Metoprolol", dose="25mg", route="PO", frequency="BID"
    )
    storage.add_referral_allergy(
        scope, referral.id, substance="Penicillin", reaction="Hives", severity="moderate"
    )
    storage.add_referral_attachment(
        scope, referral.id, kind="lab", label="CBC 2026-04-01", checklist_only=True
    )

    resp = client.get(f"/referrals/{referral.id}/export.pdf")
    assert resp.status_code == 200
    assert resp.content.startswith(b"%PDF-")


def test_render_summary_with_sub_entities_is_larger():
    """Pin ``generated_at`` so PDF metadata matches and only body bytes differ."""
    from docstats.domain.referrals import (
        ReferralAllergy,
        ReferralAttachment,
        ReferralDiagnosis,
        ReferralMedication,
    )

    patient, referral, now = _fixture_patient_referral()
    bare = render_referral_summary(referral=referral, patient=patient, generated_at=now)
    enriched = render_referral_summary(
        referral=referral,
        patient=patient,
        diagnoses=[
            ReferralDiagnosis(
                id=1,
                referral_id=referral.id,
                icd10_code="R07.9",
                icd10_desc="Chest pain unspecified",
                is_primary=True,
                source="user_entered",
                created_at=now,
            )
        ],
        medications=[
            ReferralMedication(
                id=1,
                referral_id=referral.id,
                name="Metoprolol",
                dose="25mg",
                route="PO",
                frequency="BID",
                source="user_entered",
                created_at=now,
            )
        ],
        allergies=[
            ReferralAllergy(
                id=1,
                referral_id=referral.id,
                substance="Penicillin",
                reaction="Hives",
                severity="moderate",
                source="user_entered",
                created_at=now,
            )
        ],
        attachments=[
            ReferralAttachment(
                id=1,
                referral_id=referral.id,
                kind="lab",
                label="CBC 2026-04-01",
                checklist_only=True,
                source="user_entered",
                created_at=now,
            )
        ],
        generated_at=now,
    )
    assert len(enriched) > len(bare)


def test_export_missing_patient_409(solo_client):
    """Soft-deleting the patient after referral creation surfaces a 409."""
    client, storage, user_id = solo_client
    scope = Scope(user_id=user_id)
    _, referral = _seed_referral(storage, user_id)
    # Force the patient to disappear from scope while the referral row stays.
    storage.soft_delete_patient(scope, referral.patient_id)
    resp = client.get(f"/referrals/{referral.id}/export.pdf")
    assert resp.status_code == 409


def test_export_missing_referral_404(solo_client):
    client, _, _ = solo_client
    resp = client.get("/referrals/999999/export.pdf")
    assert resp.status_code == 404


# ---------- Scope isolation ----------


def test_cross_tenant_referral_export_404(tmp_path: Path):
    """A solo user cannot export another user's referral by guessing the ID."""
    storage = Storage(db_path=tmp_path / "test.db")

    tenant_a_id = storage.create_user("a@example.com", "pw")
    tenant_b_id = storage.create_user("b@example.com", "pw")
    for uid in (tenant_a_id, tenant_b_id):
        storage.record_phi_consent(
            user_id=uid,
            phi_consent_version=CURRENT_PHI_CONSENT_VERSION,
            ip_address="",
            user_agent="",
        )

    _, referral_a = _seed_referral(storage, tenant_a_id)

    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_current_user] = lambda: _fake_user(
        tenant_b_id, email="b@example.com"
    )
    try:
        client = TestClient(app)
        resp = client.get(f"/referrals/{referral_a.id}/export.pdf")
        assert resp.status_code == 404
    finally:
        app.dependency_overrides.clear()


# ---------- PHI-consent gate ----------


def test_export_requires_phi_consent(tmp_path: Path):
    storage = Storage(db_path=tmp_path / "test.db")
    user_id = storage.create_user("a@example.com", "pw")
    # No record_phi_consent — the user has not accepted the PHI gate.
    _, referral = _seed_referral(storage, user_id)

    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_current_user] = lambda: _fake_user(user_id, consent=False)
    try:
        client = TestClient(app)
        resp = client.get(f"/referrals/{referral.id}/export.pdf", follow_redirects=False)
        assert resp.status_code in (302, 303, 307)
        assert "/auth/login" in resp.headers.get("location", "")
    finally:
        app.dependency_overrides.clear()


# ==========================================================================
# Phase 5.B — Scheduling / Patient / Attachments / Missing-Info artifacts
# ==========================================================================


# ---------- Unit tests: each new renderer returns valid PDF bytes ----------


def test_render_scheduling_summary_bytes():
    patient, referral, now = _fixture_patient_referral()
    pdf = render_scheduling_summary(
        referral=referral, patient=patient, generated_at=now, generated_by_label="Tester"
    )
    assert pdf.startswith(b"%PDF-")
    assert len(pdf) > 2000


def test_render_patient_summary_bytes():
    patient, referral, now = _fixture_patient_referral()
    pdf = render_patient_summary(
        referral=referral, patient=patient, generated_at=now, generated_by_label="Tester"
    )
    assert pdf.startswith(b"%PDF-")
    assert len(pdf) > 2000


def test_render_attachments_checklist_bytes():
    from docstats.domain.referrals import ReferralAttachment

    patient, referral, now = _fixture_patient_referral()
    attachments = [
        ReferralAttachment(
            id=1,
            referral_id=referral.id,
            kind="lab",
            label="CBC 2026-04-01",
            checklist_only=False,
            source="user_entered",
            created_at=now,
        ),
        ReferralAttachment(
            id=2,
            referral_id=referral.id,
            kind="imaging",
            label="Echo report",
            checklist_only=True,
            source="user_entered",
            created_at=now,
        ),
    ]
    pdf = render_attachments_checklist(
        referral=referral,
        patient=patient,
        attachments=attachments,
        generated_at=now,
    )
    assert pdf.startswith(b"%PDF-")


def test_render_attachments_checklist_empty_state():
    """No attachments should still render cleanly with a helpful empty state."""
    patient, referral, now = _fixture_patient_referral()
    pdf = render_attachments_checklist(
        referral=referral, patient=patient, attachments=None, generated_at=now
    )
    assert pdf.startswith(b"%PDF-")


def test_render_missing_info_bytes():
    patient, referral, now = _fixture_patient_referral()
    report = CompletenessReportV2(
        items=[
            CompletenessItem(
                code="primary_diagnosis",
                label="Primary diagnosis",
                required=True,
                satisfied=False,
            ),
            CompletenessItem(
                code="reason",
                label="Reason for referral",
                required=True,
                satisfied=True,
            ),
        ],
        red_flags=["chest pain"],
        recommended_attachments=["ECG within 30 days"],
        rejection_hints=["Missing insurance card"],
        specialty_display_name="Cardiology",
    )
    pdf = render_missing_info(
        referral=referral, patient=patient, completeness=report, generated_at=now
    )
    assert pdf.startswith(b"%PDF-")
    # Incomplete reports should render larger than minimal complete ones
    # because they emit the red-flag + required-missing sections.
    complete_report = CompletenessReportV2(
        items=[
            CompletenessItem(
                code="reason",
                label="Reason for referral",
                required=True,
                satisfied=True,
            ),
        ],
        red_flags=[],
        recommended_attachments=[],
        rejection_hints=[],
    )
    smaller = render_missing_info(
        referral=referral, patient=patient, completeness=complete_report, generated_at=now
    )
    assert len(pdf) > len(smaller)


# ---------- Route tests: each artifact returns a PDF + correct filename stem ----------


@pytest.mark.parametrize(
    "artifact,expected_stem",
    [
        ("summary", "summary"),
        ("scheduling", "scheduling"),
        ("patient", "patient"),
        ("attachments", "attachments"),
        ("missing_info", "missing-info"),
    ],
)
def test_export_route_all_artifacts(solo_client, artifact, expected_stem):
    client, storage, user_id = solo_client
    _, referral = _seed_referral(storage, user_id)

    resp = client.get(f"/referrals/{referral.id}/export.pdf", params={"artifact": artifact})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.content.startswith(b"%PDF-")
    assert f"referral-{referral.id}-{expected_stem}.pdf" in resp.headers["content-disposition"]
    assert resp.headers.get("cache-control") == "private, no-store"


def test_export_missing_info_surfaces_completeness(solo_client):
    """Missing-info artifact exercises the rules engine end-to-end."""
    client, storage, user_id = solo_client
    _, referral = _seed_referral(storage, user_id)
    resp = client.get(f"/referrals/{referral.id}/export.pdf", params={"artifact": "missing_info"})
    assert resp.status_code == 200
    assert resp.content.startswith(b"%PDF-")


def test_export_audit_captures_artifact_name(solo_client):
    """Each artifact kind should appear in the audit log by name."""
    client, storage, user_id = solo_client
    _, referral = _seed_referral(storage, user_id)

    for artifact in ("summary", "scheduling", "patient", "attachments", "missing_info"):
        resp = client.get(f"/referrals/{referral.id}/export.pdf", params={"artifact": artifact})
        assert resp.status_code == 200

    audit_rows = storage.list_audit_events(limit=50)
    export_rows = [a for a in audit_rows if a.action == "referral.export"]
    artifacts_logged = {a.metadata.get("artifact") for a in export_rows}
    assert {"summary", "scheduling", "patient", "attachments", "missing_info"} <= artifacts_logged
