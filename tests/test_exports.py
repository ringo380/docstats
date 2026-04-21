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


def _seed_referral(storage: Storage, user_id: int, *, first_name: str = "Jane"):
    scope = Scope(user_id=user_id)
    patient = storage.create_patient(
        scope,
        first_name=first_name,
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


# ==========================================================================
# Phase 5.C — Fax cover + packet bundling + preview UI
# ==========================================================================


# ---------- Unit: fax cover renderer ----------


def test_render_fax_cover_bytes():
    from docstats.exports import render_fax_cover

    patient, referral, now = _fixture_patient_referral()
    pdf = render_fax_cover(
        referral=referral,
        patient=patient,
        total_pages=3,
        generated_at=now,
        generated_by_label="Tester",
    )
    assert pdf.startswith(b"%PDF-")
    assert len(pdf) > 2000


def test_render_fax_cover_total_pages_optional():
    from docstats.exports import render_fax_cover

    patient, referral, now = _fixture_patient_referral()
    pdf = render_fax_cover(referral=referral, patient=patient, generated_at=now)
    assert pdf.startswith(b"%PDF-")


# ---------- Unit: packet bundling ----------


def test_render_packet_concatenates():
    from docstats.exports import render_fax_cover, render_packet, render_referral_summary

    patient, referral, now = _fixture_patient_referral()
    cover = render_fax_cover(referral=referral, patient=patient, generated_at=now)
    summary = render_referral_summary(referral=referral, patient=patient, generated_at=now)
    packet = render_packet(
        referral=referral, patient=patient, parts=[cover, summary], generated_at=now
    )
    assert packet.startswith(b"%PDF-")
    # Packet must be at least ~90% the sum of its parts (pypdf drops a
    # small amount of redundant PDF overhead when merging).
    assert len(packet) > 0.85 * (len(cover) + len(summary))


def test_render_packet_empty_raises():
    from docstats.exports import render_packet

    patient, referral, now = _fixture_patient_referral()
    with pytest.raises(ValueError):
        render_packet(referral=referral, patient=patient, parts=[], generated_at=now)


def test_render_packet_single_pass_through():
    """A one-part packet should return the original bytes unchanged."""
    from docstats.exports import render_fax_cover, render_packet

    patient, referral, now = _fixture_patient_referral()
    cover = render_fax_cover(referral=referral, patient=patient, generated_at=now)
    packet = render_packet(referral=referral, patient=patient, parts=[cover], generated_at=now)
    assert packet == cover


# ---------- Route: fax cover ----------


def test_export_route_fax_cover(solo_client):
    client, storage, user_id = solo_client
    _, referral = _seed_referral(storage, user_id)

    resp = client.get(f"/referrals/{referral.id}/export.pdf?artifact=fax_cover")
    assert resp.status_code == 200
    assert resp.content.startswith(b"%PDF-")
    assert f"referral-{referral.id}-fax-cover.pdf" in resp.headers["content-disposition"]


# ---------- Route: packet bundling ----------


def test_export_route_packet_default_include(solo_client):
    """Default packet includes fax_cover + summary + attachments."""
    client, storage, user_id = solo_client
    _, referral = _seed_referral(storage, user_id)

    resp = client.get(f"/referrals/{referral.id}/export.pdf?artifact=packet")
    assert resp.status_code == 200
    assert resp.content.startswith(b"%PDF-")
    assert f"referral-{referral.id}-packet.pdf" in resp.headers["content-disposition"]

    # Audit should mention the ordered include list.
    audit_rows = storage.list_audit_events(limit=20)
    export_rows = [a for a in audit_rows if a.action == "referral.export"]
    assert any(a.metadata.get("artifact", "").startswith("packet:") for a in export_rows), [
        a.metadata.get("artifact") for a in export_rows
    ]


def test_export_route_packet_custom_include(solo_client):
    client, storage, user_id = solo_client
    _, referral = _seed_referral(storage, user_id)

    resp = client.get(
        f"/referrals/{referral.id}/export.pdf",
        params={"artifact": "packet", "include": "summary,attachments"},
    )
    assert resp.status_code == 200
    assert resp.content.startswith(b"%PDF-")


def test_export_route_packet_unknown_include_400(solo_client):
    client, storage, user_id = solo_client
    _, referral = _seed_referral(storage, user_id)

    resp = client.get(
        f"/referrals/{referral.id}/export.pdf",
        params={"artifact": "packet", "include": "summary,bogus"},
    )
    assert resp.status_code == 400


def test_export_route_packet_nested_rejected(solo_client):
    """A packet can't include itself."""
    client, storage, user_id = solo_client
    _, referral = _seed_referral(storage, user_id)

    resp = client.get(
        f"/referrals/{referral.id}/export.pdf",
        params={"artifact": "packet", "include": "packet"},
    )
    assert resp.status_code == 400


def test_export_route_packet_dedupes_include(solo_client):
    """Duplicate tokens in include should not produce duplicate pages."""
    client, storage, user_id = solo_client
    _, referral = _seed_referral(storage, user_id)

    single = client.get(
        f"/referrals/{referral.id}/export.pdf",
        params={"artifact": "packet", "include": "summary"},
    )
    doubled = client.get(
        f"/referrals/{referral.id}/export.pdf",
        params={"artifact": "packet", "include": "summary,summary"},
    )
    assert single.status_code == 200
    assert doubled.status_code == 200
    # Both should be one-artifact packets (pypdf pass-through); byte
    # lengths match within the render-timestamp noise floor.
    assert abs(len(single.content) - len(doubled.content)) < 200


# ---------- Route: preview UI ----------


def test_export_preview_page(solo_client):
    client, storage, user_id = solo_client
    _, referral = _seed_referral(storage, user_id)

    resp = client.get(f"/referrals/{referral.id}/export")
    assert resp.status_code == 200
    assert "Export Referral #" in resp.text
    assert "Download packet" in resp.text
    assert "Fax Cover Sheet" in resp.text
    assert "Referral Request Summary" in resp.text
    # The form should point at export.pdf with artifact=packet.
    assert 'action="/referrals/' in resp.text
    assert 'value="packet"' in resp.text


def test_export_preview_page_missing_referral_404(solo_client):
    client, _, _ = solo_client
    resp = client.get("/referrals/999999/export")
    assert resp.status_code == 404


def test_export_preview_page_requires_phi_consent(tmp_path: Path):
    storage = Storage(db_path=tmp_path / "test.db")
    user_id = storage.create_user("a@example.com", "pw")
    _, referral = _seed_referral(storage, user_id)

    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_current_user] = lambda: _fake_user(user_id, consent=False)
    try:
        client = TestClient(app)
        resp = client.get(f"/referrals/{referral.id}/export", follow_redirects=False)
        assert resp.status_code in (302, 303, 307)
    finally:
        app.dependency_overrides.clear()


# ==========================================================================
# Phase 5.D — FHIR-ish JSON export
# ==========================================================================


def test_build_referral_bundle_smoke():
    """The FHIR bundle builder returns the expected resource types and status map."""
    from docstats.domain.referrals import (
        ReferralAllergy,
        ReferralAttachment,
        ReferralMedication,
    )
    from docstats.exports import build_referral_bundle

    patient, referral, now = _fixture_patient_referral()
    meds = [
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
    ]
    allergies = [
        ReferralAllergy(
            id=1,
            referral_id=referral.id,
            substance="Penicillin",
            reaction="Hives",
            severity="moderate",
            source="user_entered",
            created_at=now,
        )
    ]
    atts = [
        ReferralAttachment(
            id=1,
            referral_id=referral.id,
            kind="lab",
            label="CBC",
            checklist_only=False,
            source="user_entered",
            created_at=now,
        ),
        ReferralAttachment(
            id=2,
            referral_id=referral.id,
            kind="imaging",
            label="Echo",
            checklist_only=True,
            source="user_entered",
            created_at=now,
        ),
    ]

    bundle = build_referral_bundle(
        referral=referral,
        patient=patient,
        medications=meds,
        allergies=allergies,
        attachments=atts,
        generated_at=now,
    )

    assert bundle["resourceType"] == "Bundle"
    assert bundle["type"] == "document"
    types = [e["resource"]["resourceType"] for e in bundle["entry"]]
    # Order matters: Patient first, ServiceRequest second — downstream
    # consumers rely on it.
    assert types[0] == "Patient"
    assert types[1] == "ServiceRequest"
    # All expected resources present.
    assert "Practitioner" in types  # referring_provider_* populated
    assert "Organization" in types  # receiving_organization_name populated
    assert "Condition" in types  # diagnosis_primary_* populated
    assert "MedicationStatement" in types
    assert "AllergyIntolerance" in types
    # Two attachments → two DocumentReferences.
    assert types.count("DocumentReference") == 2
    # Status + priority correctly mapped.
    sr = bundle["entry"][1]["resource"]
    assert sr["status"] == "active"  # our "ready" → FHIR "active"
    assert sr["priority"] == "urgent"
    # Checklist-only attachment must be preliminary, included one must
    # be current.
    doc_refs = [
        e["resource"]
        for e in bundle["entry"]
        if e["resource"]["resourceType"] == "DocumentReference"
    ]
    statuses = sorted(d["status"] for d in doc_refs)
    assert statuses == ["current", "preliminary"]


def test_build_referral_bundle_minimal():
    """A referral with only required fields should still produce a valid bundle."""
    from datetime import datetime, timezone as tz

    from docstats.domain.patients import Patient as P
    from docstats.domain.referrals import Referral as R
    from docstats.exports import build_referral_bundle

    now = datetime(2026, 4, 20, tzinfo=tz.utc)
    patient = P(
        id=1, scope_user_id=1, first_name="John", last_name="Roe", created_at=now, updated_at=now
    )
    referral = R(id=1, scope_user_id=1, patient_id=1, created_at=now, updated_at=now)
    bundle = build_referral_bundle(referral=referral, patient=patient, generated_at=now)
    types = [e["resource"]["resourceType"] for e in bundle["entry"]]
    # Only Patient + ServiceRequest are always emitted — everything
    # else is optional.
    assert types == ["Patient", "ServiceRequest"]


def test_build_referral_bundle_status_map():
    """Every status in STATUS_VALUES should map to a valid FHIR status."""
    from docstats.domain.referrals import STATUS_VALUES
    from docstats.exports.fhir import _STATUS_MAP

    valid_fhir = {
        "draft",
        "active",
        "on-hold",
        "revoked",
        "completed",
        "entered-in-error",
        "unknown",
    }
    for status in STATUS_VALUES:
        mapped = _STATUS_MAP.get(status, "unknown")
        assert mapped in valid_fhir, f"{status} → {mapped} is not a FHIR ServiceRequest.status"


# ---------- Route: JSON export ----------


def test_export_json_happy_path(solo_client):
    client, storage, user_id = solo_client
    _, referral = _seed_referral(storage, user_id)

    resp = client.get(f"/referrals/{referral.id}/export.json")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    assert f"referral-{referral.id}-bundle.json" in resp.headers["content-disposition"]
    assert resp.headers.get("cache-control") == "private, no-store"
    bundle = resp.json()
    assert bundle["resourceType"] == "Bundle"
    assert bundle["type"] == "document"
    assert any(e["resource"]["resourceType"] == "Patient" for e in bundle["entry"])
    assert any(e["resource"]["resourceType"] == "ServiceRequest" for e in bundle["entry"])


def test_export_json_audit_and_event(solo_client):
    client, storage, user_id = solo_client
    _, referral = _seed_referral(storage, user_id)

    resp = client.get(f"/referrals/{referral.id}/export.json")
    assert resp.status_code == 200

    audit_rows = storage.list_audit_events(limit=10)
    json_audits = [
        a
        for a in audit_rows
        if a.action == "referral.export"
        and a.metadata.get("format") == "json"
        and a.metadata.get("artifact") == "fhir_bundle"
    ]
    assert len(json_audits) == 1
    # Bundle entry count should be recorded for observability.
    assert isinstance(json_audits[0].metadata.get("entries"), int)
    assert json_audits[0].metadata["entries"] >= 2

    events = storage.list_referral_events(Scope(user_id=user_id), referral.id, limit=20)
    assert any(e.event_type == "exported" and "fhir_bundle" in (e.note or "") for e in events)


def test_export_json_missing_referral_404(solo_client):
    client, _, _ = solo_client
    resp = client.get("/referrals/999999/export.json")
    assert resp.status_code == 404


def test_export_json_cross_tenant_404(tmp_path: Path):
    storage = Storage(db_path=tmp_path / "test.db")
    tenant_a = storage.create_user("a@example.com", "pw")
    tenant_b = storage.create_user("b@example.com", "pw")
    for uid in (tenant_a, tenant_b):
        storage.record_phi_consent(
            user_id=uid,
            phi_consent_version=CURRENT_PHI_CONSENT_VERSION,
            ip_address="",
            user_agent="",
        )
    _, referral_a = _seed_referral(storage, tenant_a)

    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_current_user] = lambda: _fake_user(tenant_b, email="b@example.com")
    try:
        client = TestClient(app)
        resp = client.get(f"/referrals/{referral_a.id}/export.json")
        assert resp.status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_export_json_requires_phi_consent(tmp_path: Path):
    storage = Storage(db_path=tmp_path / "test.db")
    user_id = storage.create_user("a@example.com", "pw")
    _, referral = _seed_referral(storage, user_id)

    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_current_user] = lambda: _fake_user(user_id, consent=False)
    try:
        client = TestClient(app)
        resp = client.get(f"/referrals/{referral.id}/export.json", follow_redirects=False)
        assert resp.status_code in (302, 303, 307)
    finally:
        app.dependency_overrides.clear()


# ==========================================================================
# Phase 5.E — flat CSV + batch PDF
# ==========================================================================


# ---------- CSV export ----------


def test_csv_export_happy_path(solo_client):
    import csv as csv_mod

    client, storage, user_id = solo_client
    _, referral_a = _seed_referral(storage, user_id)
    _, referral_b = _seed_referral(storage, user_id, first_name="Alice")
    _ = referral_a, referral_b

    resp = client.get("/referrals/export.csv")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "attachment" in resp.headers["content-disposition"]
    assert "referrals-" in resp.headers["content-disposition"]

    reader = csv_mod.DictReader(resp.text.splitlines())
    rows = list(reader)
    assert len(rows) == 2
    # Every column exists on every row.
    from docstats.exports import CSV_FIELDNAMES

    for row in rows:
        assert set(row.keys()) == set(CSV_FIELDNAMES)
    # Headers include patient fields, not just referral fields.
    assert "patient_first_name" in rows[0]
    assert rows[0]["patient_first_name"] in {"Jane", "Alice"}


def test_csv_export_empty_state(solo_client):
    """Zero referrals should still emit a header-only CSV."""
    client, _, _ = solo_client
    resp = client.get("/referrals/export.csv")
    assert resp.status_code == 200
    # Header line present; no data rows.
    lines = resp.text.strip().splitlines()
    assert len(lines) == 1
    assert "referral_id" in lines[0]
    assert "patient_first_name" in lines[0]


def test_csv_export_status_filter(solo_client):
    import csv as csv_mod

    client, storage, user_id = solo_client
    _, referral_draft = _seed_referral(storage, user_id, first_name="Drafty")
    _, referral_ready = _seed_referral(storage, user_id, first_name="Ready")
    storage.set_referral_status(Scope(user_id=user_id), referral_ready.id, "ready")
    _ = referral_draft

    resp = client.get("/referrals/export.csv", params={"status": "ready"})
    assert resp.status_code == 200
    rows = list(csv_mod.DictReader(resp.text.splitlines()))
    assert len(rows) == 1
    assert rows[0]["status"] == "ready"


def test_csv_export_assignee_me_filter_matches_workspace(solo_client):
    import csv as csv_mod

    client, storage, user_id = solo_client
    scope = Scope(user_id=user_id)
    _, assigned = _seed_referral(storage, user_id, first_name="Assigned")
    _, unassigned = _seed_referral(storage, user_id, first_name="Unassigned")
    storage.update_referral(scope, assigned.id, assigned_to_user_id=user_id)
    _ = unassigned

    resp = client.get("/referrals/export.csv", params={"assignee": "me"})
    assert resp.status_code == 200
    rows = list(csv_mod.DictReader(resp.text.splitlines()))
    assert len(rows) == 1
    assert rows[0]["referral_id"] == str(assigned.id)
    assert rows[0]["patient_first_name"] == "Assigned"


def test_csv_export_audit(solo_client):
    client, storage, user_id = solo_client
    _seed_referral(storage, user_id)

    resp = client.get("/referrals/export.csv")
    assert resp.status_code == 200

    audit_rows = storage.list_audit_events(limit=10)
    csv_audits = [
        a for a in audit_rows if a.action == "referral.export" and a.metadata.get("format") == "csv"
    ]
    assert len(csv_audits) == 1
    assert csv_audits[0].metadata.get("artifact") == "referrals_csv"
    assert csv_audits[0].metadata.get("rows") == 1


def test_csv_export_cross_tenant(tmp_path: Path):
    """A solo user only sees their own referrals in the CSV."""
    import csv as csv_mod

    storage = Storage(db_path=tmp_path / "test.db")
    a = storage.create_user("a@example.com", "pw")
    b = storage.create_user("b@example.com", "pw")
    for uid in (a, b):
        storage.record_phi_consent(
            user_id=uid,
            phi_consent_version=CURRENT_PHI_CONSENT_VERSION,
            ip_address="",
            user_agent="",
        )
    _seed_referral(storage, a, first_name="Alice")
    _seed_referral(storage, b, first_name="Bob")

    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_current_user] = lambda: _fake_user(a, email="a@example.com")
    try:
        client = TestClient(app)
        resp = client.get("/referrals/export.csv")
        assert resp.status_code == 200
        rows = list(csv_mod.DictReader(resp.text.splitlines()))
        assert len(rows) == 1
        assert rows[0]["patient_first_name"] == "Alice"
    finally:
        app.dependency_overrides.clear()


def test_csv_export_requires_phi_consent(tmp_path: Path):
    storage = Storage(db_path=tmp_path / "test.db")
    user_id = storage.create_user("a@example.com", "pw")
    _seed_referral(storage, user_id)

    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_current_user] = lambda: _fake_user(user_id, consent=False)
    try:
        client = TestClient(app)
        resp = client.get("/referrals/export.csv", follow_redirects=False)
        assert resp.status_code in (302, 303, 307)
    finally:
        app.dependency_overrides.clear()


# ---------- Batch PDF export ----------


def test_batch_export_happy_path(solo_client):
    client, storage, user_id = solo_client
    _, r1 = _seed_referral(storage, user_id)
    _, r2 = _seed_referral(storage, user_id, first_name="Alice")

    resp = client.post(
        "/referrals/batch-export.pdf",
        data={"referral_ids": f"{r1.id},{r2.id}"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.content.startswith(b"%PDF-")
    assert "attachment" in resp.headers["content-disposition"]
    assert "referrals-batch-" in resp.headers["content-disposition"]

    rendered = resp.headers.get("x-export-rendered", "")
    assert str(r1.id) in rendered
    assert str(r2.id) in rendered


def test_batch_export_dedupes_referral_ids(solo_client):
    client, storage, user_id = solo_client
    _, r = _seed_referral(storage, user_id)

    single = client.post(
        "/referrals/batch-export.pdf",
        data={"referral_ids": str(r.id)},
    )
    dupe = client.post(
        "/referrals/batch-export.pdf",
        data={"referral_ids": f"{r.id},{r.id}"},
    )
    assert single.status_code == 200
    assert dupe.status_code == 200
    # Rendered-id header should list the id exactly once after dedup.
    assert dupe.headers.get("x-export-rendered") == str(r.id)


def test_batch_export_rejects_oversized(solo_client):
    client, storage, user_id = solo_client
    _seed_referral(storage, user_id)

    resp = client.post(
        "/referrals/batch-export.pdf",
        data={"referral_ids": ",".join(str(i + 1) for i in range(51))},
    )
    assert resp.status_code == 400


def test_batch_export_skips_missing_ids(solo_client):
    client, storage, user_id = solo_client
    _, r = _seed_referral(storage, user_id)

    resp = client.post(
        "/referrals/batch-export.pdf",
        data={"referral_ids": f"{r.id},9999999"},
    )
    assert resp.status_code == 200
    assert str(r.id) in resp.headers.get("x-export-rendered", "")
    assert "9999999" in resp.headers.get("x-export-skipped", "")


def test_batch_export_all_missing_returns_404(solo_client):
    client, _, _ = solo_client
    resp = client.post(
        "/referrals/batch-export.pdf",
        data={"referral_ids": "9999998,9999999"},
    )
    assert resp.status_code == 404


def test_batch_export_unknown_artifact_400(solo_client):
    client, storage, user_id = solo_client
    _, r = _seed_referral(storage, user_id)
    resp = client.post(
        "/referrals/batch-export.pdf",
        data={"referral_ids": str(r.id), "artifact": "bogus"},
    )
    assert resp.status_code == 400


def test_batch_export_audit_captures_ids(solo_client):
    client, storage, user_id = solo_client
    _, r = _seed_referral(storage, user_id)

    resp = client.post("/referrals/batch-export.pdf", data={"referral_ids": str(r.id)})
    assert resp.status_code == 200

    audit_rows = storage.list_audit_events(limit=10)
    batch_audits = [
        a
        for a in audit_rows
        if a.action == "referral.export"
        and isinstance(a.metadata.get("artifact", ""), str)
        and a.metadata["artifact"].startswith("batch:")
    ]
    assert len(batch_audits) == 1
    assert batch_audits[0].metadata.get("rendered") == [r.id]


def test_batch_export_cross_tenant_skips(tmp_path: Path):
    """Referrals from another tenant should be skipped, not leaked."""
    storage = Storage(db_path=tmp_path / "test.db")
    a = storage.create_user("a@example.com", "pw")
    b = storage.create_user("b@example.com", "pw")
    for uid in (a, b):
        storage.record_phi_consent(
            user_id=uid,
            phi_consent_version=CURRENT_PHI_CONSENT_VERSION,
            ip_address="",
            user_agent="",
        )
    _, r_a = _seed_referral(storage, a)
    _, r_b = _seed_referral(storage, b)

    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_current_user] = lambda: _fake_user(b, email="b@example.com")
    try:
        client = TestClient(app)
        resp = client.post(
            "/referrals/batch-export.pdf",
            data={"referral_ids": f"{r_a.id},{r_b.id}"},
        )
        assert resp.status_code == 200
        assert resp.headers.get("x-export-rendered") == str(r_b.id)
        assert str(r_a.id) in resp.headers.get("x-export-skipped", "")
    finally:
        app.dependency_overrides.clear()
