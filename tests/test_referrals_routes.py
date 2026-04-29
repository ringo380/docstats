"""Route-level tests for the referrals workspace (Phase 2.B)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from docstats.auth import get_current_user
from docstats.phi import CURRENT_PHI_CONSENT_VERSION
from docstats.scope import Scope
from docstats.storage import Storage, _to_sqlite_utc_iso, get_storage
from docstats.web import app


def _fake_user(
    user_id: int,
    email: str = "a@example.com",
    *,
    consent: bool = True,
    active_org_id: int | None = None,
):
    return {
        "id": user_id,
        "email": email,
        "display_name": None,
        "first_name": None,
        "last_name": None,
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
        "active_org_id": active_org_id,
    }


def _seed_referral(storage: Storage, user_id: int, **overrides):
    scope = Scope(user_id=user_id)
    patient = storage.create_patient(
        scope,
        first_name=overrides.pop("first_name", "Jane"),
        last_name=overrides.pop("last_name", "Doe"),
        date_of_birth="1980-05-15",
        created_by_user_id=user_id,
    )
    return storage.create_referral(
        scope,
        patient_id=patient.id,
        reason=overrides.pop("reason", "Chest pain eval"),
        urgency=overrides.pop("urgency", "routine"),
        specialty_desc=overrides.pop("specialty_desc", "Cardiology"),
        receiving_organization_name=overrides.pop("receiving_organization_name", "Heart Clinic"),
        created_by_user_id=user_id,
        **overrides,
    )


def _seed_org_referral(storage: Storage, user_id: int, org_id: int, role: str, **overrides):
    scope = Scope(user_id=user_id, organization_id=org_id, membership_role=role)
    patient = storage.create_patient(
        scope,
        first_name=overrides.pop("first_name", "Jane"),
        last_name=overrides.pop("last_name", "Doe"),
        date_of_birth="1980-05-15",
        created_by_user_id=user_id,
    )
    return storage.create_referral(
        scope,
        patient_id=patient.id,
        reason=overrides.pop("reason", "Chest pain eval"),
        urgency=overrides.pop("urgency", "routine"),
        specialty_desc=overrides.pop("specialty_desc", "Cardiology"),
        receiving_organization_name=overrides.pop("receiving_organization_name", "Heart Clinic"),
        created_by_user_id=user_id,
        **overrides,
    )


def _age_referral(storage: Storage, referral_id: int, *, days: int) -> None:
    old_updated = datetime.now(timezone.utc) - timedelta(days=days)
    storage._conn.execute(
        "UPDATE referrals SET updated_at = ? WHERE id = ?",
        (_to_sqlite_utc_iso(old_updated), referral_id),
    )
    storage._conn.commit()


@pytest.fixture
def solo_client(tmp_path: Path):
    storage = Storage(db_path=tmp_path / "test.db")
    user_id = storage.create_user("a@example.com", "hashed_pw")
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


@pytest.fixture
def org_client_factory(tmp_path: Path):
    def make(role: str):
        storage = Storage(db_path=tmp_path / f"{role}.db")
        user_id = storage.create_user(f"{role}@example.com", "hashed_pw")
        storage.record_phi_consent(
            user_id=user_id,
            phi_consent_version=CURRENT_PHI_CONSENT_VERSION,
            ip_address="",
            user_agent="",
        )
        org = storage.create_organization(name="Clinic", slug=f"clinic-{role}")
        storage.create_membership(organization_id=org.id, user_id=user_id, role=role)
        storage.set_active_org(user_id, org.id)
        app.dependency_overrides[get_storage] = lambda: storage
        app.dependency_overrides[get_current_user] = lambda: _fake_user(
            user_id,
            f"{role}@example.com",
            active_org_id=org.id,
        )
        return TestClient(app), storage, user_id, org.id

    yield make
    app.dependency_overrides.clear()


# --- Empty state + consent gate ---


def test_workspace_empty(solo_client):
    client, _, _ = solo_client
    resp = client.get("/referrals")
    assert resp.status_code == 200
    assert "No referrals yet" in resp.text


def test_consent_gate(tmp_path: Path):
    storage = Storage(db_path=tmp_path / "test.db")
    user_id = storage.create_user("a@example.com", "hashed")
    # No phi_consent recorded
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_current_user] = lambda: _fake_user(user_id, consent=False)
    try:
        client = TestClient(app)
        resp = client.get("/referrals", follow_redirects=False)
        assert resp.status_code in (302, 303, 307)
        assert "/auth/login" in resp.headers.get("location", "")
    finally:
        app.dependency_overrides.clear()


# --- Population + filters ---


def test_workspace_renders_referral(solo_client):
    client, storage, user_id = solo_client
    _seed_referral(storage, user_id)
    resp = client.get("/referrals")
    assert resp.status_code == 200
    assert "Jane Doe" in resp.text
    assert "Cardiology" in resp.text
    assert "Heart Clinic" in resp.text


def test_workspace_shows_stale_waiting_banner(solo_client):
    client, storage, user_id = solo_client
    stale = _seed_referral(storage, user_id, status="awaiting_records")
    fresh = _seed_referral(storage, user_id, status="awaiting_auth")
    wrong_status = _seed_referral(storage, user_id, status="sent")
    _age_referral(storage, stale.id, days=4)
    _age_referral(storage, wrong_status.id, days=4)

    resp = client.get("/referrals")

    assert resp.status_code == 200
    assert "1 referral" in resp.text
    assert "waiting on records or authorization" in resp.text
    assert "more than 3" in resp.text
    assert storage.get_referral(Scope(user_id=user_id), fresh.id) is not None


def test_workspace_hides_stale_banner_when_no_waiting_rows(solo_client):
    client, storage, user_id = solo_client
    fresh = _seed_referral(storage, user_id, status="awaiting_auth")
    old_sent = _seed_referral(storage, user_id, status="sent")
    _age_referral(storage, old_sent.id, days=4)

    resp = client.get("/referrals")

    assert resp.status_code == 200
    assert "waiting on records or authorization" not in resp.text
    assert storage.get_referral(Scope(user_id=user_id), fresh.id) is not None


def test_filter_by_status(solo_client):
    client, storage, user_id = solo_client
    r_draft = _seed_referral(storage, user_id, first_name="Alice")
    r_ready = _seed_referral(storage, user_id, first_name="Bob")
    storage.set_referral_status(Scope(user_id=user_id), r_ready.id, "ready")
    resp = client.get("/referrals", params={"status": "ready"})
    assert resp.status_code == 200
    assert "Bob" in resp.text
    assert "Alice" not in resp.text
    _ = r_draft  # silence unused


def test_filter_by_urgency(solo_client):
    client, storage, user_id = solo_client
    _seed_referral(storage, user_id, first_name="Alice", urgency="routine")
    _seed_referral(storage, user_id, first_name="Bob", urgency="urgent")
    resp = client.get("/referrals", params={"urgency": "urgent"})
    assert resp.status_code == 200
    assert "Bob" in resp.text
    assert "Alice" not in resp.text


def test_invalid_status_filter_still_renders(solo_client):
    """Bookmarked URL with an unknown status should fall back to 'all'."""
    client, storage, user_id = solo_client
    _seed_referral(storage, user_id, first_name="Alice")
    resp = client.get("/referrals", params={"status": "not-a-status"})
    assert resp.status_code == 200
    assert "Alice" in resp.text


def test_filter_by_patient_id(solo_client):
    client, storage, user_id = solo_client
    r1 = _seed_referral(storage, user_id, first_name="Alice")
    r2 = _seed_referral(storage, user_id, first_name="Bob")
    resp = client.get("/referrals", params={"patient_id": r1.patient_id})
    assert resp.status_code == 200
    assert "Alice" in resp.text
    assert "Bob" not in resp.text
    _ = r2


# --- Cross-tenant isolation ---


# --- New form + create (Phase 2.C) ---


def test_new_form_empty_shows_patient_cta(solo_client):
    client, _, _ = solo_client
    resp = client.get("/referrals/new")
    assert resp.status_code == 200
    assert "need at least one patient" in resp.text.lower()


def test_new_form_with_patients(solo_client):
    client, storage, user_id = solo_client
    storage.create_patient(
        Scope(user_id=user_id),
        first_name="Jane",
        last_name="Doe",
        created_by_user_id=user_id,
    )
    resp = client.get("/referrals/new")
    assert resp.status_code == 200
    assert "Jane Doe" in resp.text
    assert 'name="reason"' in resp.text


def test_create_referral_redirects_to_detail(solo_client):
    client, storage, user_id = solo_client
    patient = storage.create_patient(
        Scope(user_id=user_id),
        first_name="Jane",
        last_name="Doe",
        created_by_user_id=user_id,
    )
    resp = client.post(
        "/referrals",
        data={
            "patient_id": patient.id,
            "reason": "Chest pain eval",
            "urgency": "priority",
            "specialty_desc": "Cardiology",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    loc = resp.headers["location"]
    assert loc.startswith("/referrals/")
    events = storage.list_audit_events(limit=5)
    assert any(e.action == "referral.create" for e in events)


def test_create_referral_missing_reason(solo_client):
    client, storage, user_id = solo_client
    patient = storage.create_patient(
        Scope(user_id=user_id),
        first_name="Jane",
        last_name="Doe",
        created_by_user_id=user_id,
    )
    # Whitespace-only reason should hit the route-level rejection
    resp = client.post(
        "/referrals",
        data={"patient_id": patient.id, "reason": "   ", "urgency": "routine"},
    )
    assert resp.status_code == 200
    assert "required" in resp.text.lower()


def test_create_referral_cross_scope_patient(tmp_path: Path):
    """Creating a referral against another user's patient must fail."""
    storage = Storage(db_path=tmp_path / "test.db")
    uid_a = storage.create_user("a@example.com", "hashed")
    uid_b = storage.create_user("b@example.com", "hashed")
    for uid in (uid_a, uid_b):
        storage.record_phi_consent(
            user_id=uid,
            phi_consent_version=CURRENT_PHI_CONSENT_VERSION,
            ip_address="",
            user_agent="",
        )
    patient_b = storage.create_patient(
        Scope(user_id=uid_b),
        first_name="Bob",
        last_name="Private",
        created_by_user_id=uid_b,
    )

    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_current_user] = lambda: _fake_user(uid_a)
    try:
        client = TestClient(app)
        resp = client.post(
            "/referrals",
            data={
                "patient_id": patient_b.id,
                "reason": "Trying to forge",
                "urgency": "routine",
            },
        )
        # Route catches the ValueError and re-renders with errors (200).
        assert resp.status_code == 200
        assert "not found" in resp.text.lower() or "not accessible" in resp.text.lower()
    finally:
        app.dependency_overrides.clear()


def test_create_referral_invalid_npi(solo_client):
    client, storage, user_id = solo_client
    patient = storage.create_patient(
        Scope(user_id=user_id),
        first_name="Jane",
        last_name="Doe",
        created_by_user_id=user_id,
    )
    resp = client.post(
        "/referrals",
        data={
            "patient_id": patient.id,
            "reason": "eval",
            "urgency": "routine",
            "receiving_provider_npi": "abc",
        },
    )
    assert resp.status_code == 422


# --- Detail (Phase 2.C) ---


def test_detail_renders(solo_client):
    client, storage, user_id = solo_client
    r = _seed_referral(storage, user_id)
    resp = client.get(f"/referrals/{r.id}")
    assert resp.status_code == 200
    assert "Jane Doe" in resp.text
    assert "Chest pain eval" in resp.text


def test_detail_not_found(solo_client):
    client, _, _ = solo_client
    resp = client.get("/referrals/99999")
    assert resp.status_code == 404


def test_detail_cross_tenant_404(tmp_path: Path):
    storage = Storage(db_path=tmp_path / "test.db")
    uid_a = storage.create_user("a@example.com", "hashed")
    uid_b = storage.create_user("b@example.com", "hashed")
    for uid in (uid_a, uid_b):
        storage.record_phi_consent(
            user_id=uid,
            phi_consent_version=CURRENT_PHI_CONSENT_VERSION,
            ip_address="",
            user_agent="",
        )
    r_b = _seed_referral(storage, uid_b, first_name="PrivateBob")
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_current_user] = lambda: _fake_user(uid_a)
    try:
        client = TestClient(app)
        resp = client.get(f"/referrals/{r_b.id}")
        assert resp.status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_cross_user_workspace_isolation(tmp_path: Path):
    storage = Storage(db_path=tmp_path / "test.db")
    uid_a = storage.create_user("a@example.com", "hashed")
    uid_b = storage.create_user("b@example.com", "hashed")
    for uid in (uid_a, uid_b):
        storage.record_phi_consent(
            user_id=uid,
            phi_consent_version=CURRENT_PHI_CONSENT_VERSION,
            ip_address="",
            user_agent="",
        )
    _seed_referral(storage, uid_b, first_name="PrivateBob")

    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_current_user] = lambda: _fake_user(uid_a)
    try:
        client = TestClient(app)
        resp = client.get("/referrals")
        assert resp.status_code == 200
        assert "PrivateBob" not in resp.text
    finally:
        app.dependency_overrides.clear()


# --- Update / status / delete / completeness (Phase 2.D) ---


def test_update_emits_one_event_per_changed_field(solo_client):
    client, storage, user_id = solo_client
    r = _seed_referral(storage, user_id)
    before = len(storage.list_referral_events(Scope(user_id=user_id), r.id))
    resp = client.post(
        f"/referrals/{r.id}",
        data={
            "reason": "Updated reason",
            "clinical_question": "New question",
            "urgency": r.urgency,  # unchanged — no event
            "specialty_desc": r.specialty_desc,  # unchanged
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    events = storage.list_referral_events(Scope(user_id=user_id), r.id)
    after = len(events)
    # 2 changed fields → 2 new events
    assert after == before + 2
    edits = [e for e in events if e.event_type == "field_edited"]
    # New semantics: note = field name, from_value = old value, to_value = new value
    notes = {e.note for e in edits}
    assert "reason" in notes
    assert "clinical_question" in notes
    reason_edit = next(e for e in edits if e.note == "reason")
    assert reason_edit.from_value == "Chest pain eval"
    assert reason_edit.to_value == "Updated reason"


def test_update_no_op(solo_client):
    """Submitting the form with all-identical values emits zero events."""
    client, storage, user_id = solo_client
    r = _seed_referral(storage, user_id)
    before = len(storage.list_referral_events(Scope(user_id=user_id), r.id))
    resp = client.post(
        f"/referrals/{r.id}",
        data={
            "reason": r.reason,
            "urgency": r.urgency,
            "specialty_desc": r.specialty_desc,
            "receiving_organization_name": r.receiving_organization_name,
        },
    )
    # No change → re-render (200), no events
    assert resp.status_code == 200
    after = len(storage.list_referral_events(Scope(user_id=user_id), r.id))
    assert after == before


def test_status_transition_allowed(solo_client):
    client, storage, user_id = solo_client
    r = _seed_referral(storage, user_id)  # draft
    resp = client.post(
        f"/referrals/{r.id}/status",
        data={"new_status": "ready"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    updated = storage.get_referral(Scope(user_id=user_id), r.id)
    assert updated.status == "ready"
    events = storage.list_referral_events(Scope(user_id=user_id), r.id)
    assert any(
        e.event_type == "status_changed" and e.from_value == "draft" and e.to_value == "ready"
        for e in events
    )


def test_org_staff_status_transition_allowed(org_client_factory):
    client, storage, user_id, org_id = org_client_factory("staff")
    r = _seed_org_referral(storage, user_id, org_id, "staff")
    scope = Scope(user_id=user_id, organization_id=org_id, membership_role="staff")

    resp = client.post(
        f"/referrals/{r.id}/status",
        data={"new_status": "ready"},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    updated = storage.get_referral(scope, r.id)
    assert updated is not None
    assert updated.status == "ready"


def test_org_read_only_status_transition_forbidden(org_client_factory):
    client, storage, user_id, org_id = org_client_factory("read_only")
    r = _seed_org_referral(storage, user_id, org_id, "read_only")
    scope = Scope(user_id=user_id, organization_id=org_id, membership_role="read_only")

    resp = client.post(
        f"/referrals/{r.id}/status",
        data={"new_status": "ready"},
        follow_redirects=False,
    )

    assert resp.status_code == 403
    updated = storage.get_referral(scope, r.id)
    assert updated is not None
    assert updated.status == "draft"
    events = storage.list_referral_events(scope, r.id)
    assert not any(e.event_type == "status_changed" for e in events)


def test_status_transition_disallowed(solo_client):
    client, storage, user_id = solo_client
    r = _seed_referral(storage, user_id)  # draft
    # draft → completed is not a legal edge.
    resp = client.post(
        f"/referrals/{r.id}/status",
        data={"new_status": "completed"},
    )
    assert resp.status_code == 200
    assert "invalid" in resp.text.lower() or "transition" in resp.text.lower()
    updated = storage.get_referral(Scope(user_id=user_id), r.id)
    assert updated.status == "draft"


def test_status_transition_unknown_status(solo_client):
    client, storage, user_id = solo_client
    r = _seed_referral(storage, user_id)
    resp = client.post(
        f"/referrals/{r.id}/status",
        data={"new_status": "on_fire"},
    )
    assert resp.status_code == 422


def test_soft_delete_referral(solo_client):
    client, storage, user_id = solo_client
    r = _seed_referral(storage, user_id)
    resp = client.delete(f"/referrals/{r.id}", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/referrals"
    assert storage.get_referral(Scope(user_id=user_id), r.id) is None
    events = storage.list_audit_events(limit=5)
    assert any(e.action == "referral.delete" for e in events)


def test_completeness_partial(solo_client):
    client, storage, user_id = solo_client
    r = _seed_referral(storage, user_id)  # has reason, specialty_desc, receiving_org
    resp = client.get(f"/referrals/{r.id}/completeness")
    assert resp.status_code == 200
    # Baseline required: reason + receiving side + specialty — all present
    assert "Required fields present" in resp.text
    # Recommended item "Specific clinical question" should be missing by default
    assert "Specific clinical question" in resp.text


def test_completeness_shows_required_missing(solo_client):
    client, storage, user_id = solo_client
    # Create a referral with only a patient + reason — missing receiving side + specialty
    scope = Scope(user_id=user_id)
    patient = storage.create_patient(
        scope, first_name="A", last_name="B", created_by_user_id=user_id
    )
    r = storage.create_referral(
        scope,
        patient_id=patient.id,
        reason="Just a reason",
        created_by_user_id=user_id,
    )
    resp = client.get(f"/referrals/{r.id}/completeness")
    assert resp.status_code == 200
    assert "required field" in resp.text.lower()


def test_create_escalates_urgency_on_red_flag(solo_client):
    """Routine urgency + a seeded red-flag keyword in reason auto-escalates
    to 'urgent' and emits a field_edited event on the timeline."""
    from docstats.domain.seed import seed_platform_defaults

    client, storage, user_id = solo_client
    seed_platform_defaults(storage)
    scope = Scope(user_id=user_id)
    patient = storage.create_patient(
        scope, first_name="Red", last_name="Flag", created_by_user_id=user_id
    )
    resp = client.post(
        "/referrals",
        data={
            "patient_id": patient.id,
            "reason": "chest pain and syncope",
            "urgency": "routine",
            "specialty_code": "207RC0000X",
            "specialty_desc": "Cardiology",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    referral_id = int(resp.headers["location"].rsplit("/", 1)[-1])
    r = storage.get_referral(scope, referral_id)
    assert r.urgency == "urgent"
    # Timeline captures the escalation
    events = storage.list_referral_events(scope, referral_id)
    assert any(
        e.event_type == "field_edited"
        and e.to_value == "urgent"
        and "auto-escalated" in (e.note or "")
        for e in events
    )


def test_create_preserves_higher_urgency(solo_client):
    """User-set urgency higher than 'routine' is never overridden by
    auto-escalation."""
    from docstats.domain.seed import seed_platform_defaults

    client, storage, user_id = solo_client
    seed_platform_defaults(storage)
    scope = Scope(user_id=user_id)
    patient = storage.create_patient(
        scope, first_name="A", last_name="B", created_by_user_id=user_id
    )
    resp = client.post(
        "/referrals",
        data={
            "patient_id": patient.id,
            "reason": "chest pain",
            "urgency": "stat",  # user picked higher than urgent
            "specialty_code": "207RC0000X",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    r = storage.get_referral(scope, int(resp.headers["location"].rsplit("/", 1)[-1]))
    assert r.urgency == "stat"  # not downgraded


def test_intake_questions_endpoint(solo_client):
    from docstats.domain.seed import seed_platform_defaults

    client, storage, _ = solo_client
    seed_platform_defaults(storage)
    resp = client.get("/referrals/intake-questions", params={"specialty_code": "207RC0000X"})
    assert resp.status_code == 200
    # Cardiology intake prompts seeded in SPECIALTY_DEFAULTS
    assert "Family history" in resp.text or "cardiac" in resp.text.lower()


def test_intake_questions_unknown_code_empty(solo_client):
    client, _, _ = solo_client
    resp = client.get("/referrals/intake-questions", params={"specialty_code": "BOGUS"})
    assert resp.status_code == 200
    # Empty specialty → template renders blank wrapper
    assert "intake-panel" not in resp.text.lower() or resp.text.strip() == ""


def test_completeness_surfaces_red_flags(solo_client):
    """With cardiology rules seeded, a chest-pain referral surfaces the
    red-flag section in the rendered partial."""
    from docstats.domain.seed import seed_platform_defaults

    client, storage, user_id = solo_client
    seed_platform_defaults(storage)
    scope = Scope(user_id=user_id)
    patient = storage.create_patient(
        scope, first_name="P", last_name="Q", created_by_user_id=user_id
    )
    r = storage.create_referral(
        scope,
        patient_id=patient.id,
        reason="New-onset chest pain with syncope",
        specialty_code="207RC0000X",
        specialty_desc="Cardiology",
        receiving_organization_name="Heart Clinic",
        created_by_user_id=user_id,
    )
    resp = client.get(f"/referrals/{r.id}/completeness")
    assert resp.status_code == 200
    assert "Red-flag" in resp.text or "red-flag" in resp.text.lower()
    assert "chest pain" in resp.text.lower()
    assert "Cardiology" in resp.text
    # Rejection hints section appears
    assert "rejection" in resp.text.lower()


# --- Enum validation on update route (review follow-up) ---


def test_update_rejects_unknown_urgency(solo_client):
    client, storage, user_id = solo_client
    r = _seed_referral(storage, user_id)
    resp = client.post(f"/referrals/{r.id}", data={"urgency": "on_fire"})
    assert resp.status_code == 422


def test_update_rejects_unknown_auth_status(solo_client):
    client, storage, user_id = solo_client
    r = _seed_referral(storage, user_id)
    resp = client.post(f"/referrals/{r.id}", data={"authorization_status": "bogus"})
    assert resp.status_code == 422


# --- Clear field route (review follow-up) ---


def test_clear_auth_number(solo_client):
    client, storage, user_id = solo_client
    r = _seed_referral(storage, user_id)
    scope = Scope(user_id=user_id)
    storage.update_referral(scope, r.id, authorization_number="AUTH-123")
    resp = client.post(
        f"/referrals/{r.id}/clear/authorization_number",
        follow_redirects=False,
    )
    assert resp.status_code == 303
    updated = storage.get_referral(scope, r.id)
    assert updated.authorization_number is None
    # Event records old → new=None with note = field name
    edits = [
        e
        for e in storage.list_referral_events(scope, r.id)
        if e.event_type == "field_edited" and e.note == "authorization_number"
    ]
    assert len(edits) == 1
    assert edits[0].from_value == "AUTH-123"
    assert edits[0].to_value is None


def test_clear_field_rejects_unlisted(solo_client):
    client, storage, user_id = solo_client
    r = _seed_referral(storage, user_id)
    # reason is not in the 4-field clearable allow-list
    resp = client.post(f"/referrals/{r.id}/clear/reason")
    assert resp.status_code == 422


def test_clear_field_not_found(solo_client):
    client, _, _ = solo_client
    resp = client.post("/referrals/99999/clear/authorization_number")
    assert resp.status_code == 404


# --- Storage urgency filter (review follow-up) ---


def test_list_referrals_urgency_filter_at_storage(solo_client):
    _client, storage, user_id = solo_client
    scope = Scope(user_id=user_id)
    _seed_referral(storage, user_id, first_name="Alice", urgency="routine")
    _seed_referral(storage, user_id, first_name="Bob", urgency="urgent")
    _seed_referral(storage, user_id, first_name="Carol", urgency="stat")
    routine_only = storage.list_referrals(scope, urgency="routine")
    assert len(routine_only) == 1
    urgent_only = storage.list_referrals(scope, urgency="urgent")
    assert len(urgent_only) == 1


# ---------------------------------------------------------------------------
# _ehr_post_create_hook — clinical import + ServiceRequest write-back
# ---------------------------------------------------------------------------


def _ehr_fixture(tmp_path: Path, monkeypatch, *, with_ehr_fhir_id: bool = True):
    """Return (client, storage, user_id) with Epic mocked for clinical imports."""
    from cryptography.fernet import Fernet
    from docstats.ehr.crypto import encrypt_token

    storage = Storage(db_path=tmp_path / "test.db")
    user_id = storage.create_user("ehr@example.com", "pw")
    storage.record_phi_consent(
        user_id=user_id,
        phi_consent_version=CURRENT_PHI_CONSENT_VERSION,
        ip_address="",
        user_agent="",
    )

    monkeypatch.setenv("EHR_EPIC_SANDBOX_ENABLED", "1")
    monkeypatch.setenv("EHR_TOKEN_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("EPIC_CLIENT_ID", "fake")
    monkeypatch.setenv("EPIC_CLIENT_SECRET", "fake")
    monkeypatch.setenv("EPIC_REDIRECT_URI", "https://referme.help/ehr/callback/epic")
    monkeypatch.setenv("EPIC_SANDBOX_BASE_URL", "https://fake-epic.test")

    from docstats.ehr import epic
    epic._DISCOVERY_CACHE.clear()
    epic._DISCOVERY_CACHE["https://fake-epic.test"] = (
        epic.EpicEndpoints(
            authorize_endpoint="https://fake-epic.test/oauth2/authorize",
            token_endpoint="https://fake-epic.test/oauth2/token",
            fhir_base="https://fake-epic.test/api/FHIR/R4",
        ),
        9999999999.0,
    )

    # Seed EHR connection.
    storage.create_ehr_connection(
        user_id=user_id,
        ehr_vendor="epic_sandbox",
        iss="https://fake-epic.test",
        access_token_enc=encrypt_token("ACCESS-TOKEN"),
        refresh_token_enc=None,
        expires_at=datetime.now(tz=timezone.utc) + timedelta(hours=1),
        scope="openid",
        patient_fhir_id="PAT-99",
    )

    scope = Scope(user_id=user_id)
    patient = storage.create_patient(
        scope,
        first_name="Sam",
        last_name="Carter",
        date_of_birth="1975-01-02",
        created_by_user_id=user_id,
        ehr_fhir_id="PAT-99" if with_ehr_fhir_id else None,
    )

    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_current_user] = lambda: _fake_user(user_id, "ehr@example.com")

    return TestClient(app), storage, user_id, patient


def _mock_clinical_resources(monkeypatch):
    """Make all Epic fetch_* calls return minimal FHIR bundles."""
    import docstats.ehr.epic as _epic

    CONDITION = {
        "resourceType": "Condition",
        "code": {
            "coding": [
                {
                    "system": "http://hl7.org/fhir/sid/icd-10-cm",
                    "code": "E11.9",
                    "display": "Type 2 DM",
                }
            ]
        },
    }
    MEDICATION = {
        "resourceType": "MedicationStatement",
        "medicationCodeableConcept": {"text": "Metformin 500mg"},
    }
    ALLERGY = {
        "resourceType": "AllergyIntolerance",
        "code": {"text": "Penicillin"},
        "reaction": [{"manifestation": [{"text": "Hives"}], "severity": "moderate"}],
    }

    monkeypatch.setattr(_epic, "fetch_conditions", lambda **_kw: [CONDITION])
    monkeypatch.setattr(_epic, "fetch_medications", lambda **_kw: [MEDICATION])
    monkeypatch.setattr(_epic, "fetch_allergies", lambda **_kw: [ALLERGY])
    monkeypatch.setattr(_epic, "fetch_document_references", lambda **_kw: [])
    monkeypatch.setattr(
        _epic,
        "write_service_request",
        lambda **_kw: "SR-001",
    )


def _post_referral(client, patient_id: int):
    return client.post(
        "/referrals",
        data={
            "patient_id": str(patient_id),
            "reason": "EHR test reason",
            "urgency": "routine",
            "specialty_desc": "Cardiology",
            "receiving_organization_name": "Heart Clinic",
        },
        follow_redirects=False,
    )


def test_ehr_hook_inserts_clinical_data_on_referral_create(tmp_path, monkeypatch):
    """Patient with ehr_fhir_id + active connection → diagnoses/meds/allergies inserted."""
    client, storage, user_id, patient = _ehr_fixture(tmp_path, monkeypatch)
    _mock_clinical_resources(monkeypatch)
    try:
        resp = _post_referral(client, patient.id)
        assert resp.status_code == 303
        location = resp.headers["location"]
        referral_id = int(location.split("/referrals/")[1])

        scope = Scope(user_id=user_id)
        diags = storage.list_referral_diagnoses(scope, referral_id)
        assert any(d.icd10_code == "E11.9" for d in diags)

        meds = storage.list_referral_medications(scope, referral_id)
        assert any("Metformin" in (m.name or "") for m in meds)

        allergies = storage.list_referral_allergies(scope, referral_id)
        assert any(a.substance == "Penicillin" for a in allergies)
    finally:
        app.dependency_overrides.clear()
        from docstats.ehr import epic
        epic._DISCOVERY_CACHE.clear()


def test_ehr_hook_skipped_when_no_ehr_fhir_id(tmp_path, monkeypatch):
    """Patient without ehr_fhir_id → hook exits early, no EHR fetch attempted."""
    import docstats.ehr.epic as _epic

    client, storage, user_id, patient = _ehr_fixture(tmp_path, monkeypatch, with_ehr_fhir_id=False)
    fetch_called = {"n": 0}

    def _fail(**_kw):
        fetch_called["n"] += 1
        return []

    monkeypatch.setattr(_epic, "fetch_conditions", _fail)
    monkeypatch.setattr(_epic, "fetch_medications", _fail)
    monkeypatch.setattr(_epic, "fetch_allergies", _fail)
    monkeypatch.setattr(_epic, "fetch_document_references", _fail)
    monkeypatch.setattr(_epic, "write_service_request", lambda **_kw: "SR-X")
    try:
        resp = _post_referral(client, patient.id)
        assert resp.status_code == 303
        assert fetch_called["n"] == 0
    finally:
        app.dependency_overrides.clear()
        _epic._DISCOVERY_CACHE.clear()


def test_ehr_hook_soft_fails_on_epic_error(tmp_path, monkeypatch):
    """EpicError on clinical fetch → referral still created, no exception surfaced."""
    from docstats.ehr.epic import EpicError
    import docstats.ehr.epic as _epic

    client, storage, user_id, patient = _ehr_fixture(tmp_path, monkeypatch)

    def _raise(**_kw):
        raise EpicError("network error")

    monkeypatch.setattr(_epic, "fetch_conditions", _raise)
    monkeypatch.setattr(_epic, "fetch_medications", _raise)
    monkeypatch.setattr(_epic, "fetch_allergies", _raise)
    monkeypatch.setattr(_epic, "fetch_document_references", _raise)
    monkeypatch.setattr(_epic, "write_service_request", lambda **_kw: "SR-OK")
    try:
        resp = _post_referral(client, patient.id)
        # Referral creation must succeed even if hook fails.
        assert resp.status_code == 303
        assert "/referrals/" in resp.headers["location"]
    finally:
        app.dependency_overrides.clear()
        _epic._DISCOVERY_CACHE.clear()


def test_ehr_hook_service_request_sets_id_on_referral(tmp_path, monkeypatch):
    """Successful ServiceRequest write → ehr_service_request_id set on referral."""
    client, storage, user_id, patient = _ehr_fixture(tmp_path, monkeypatch)
    _mock_clinical_resources(monkeypatch)
    try:
        resp = _post_referral(client, patient.id)
        assert resp.status_code == 303
        referral_id = int(resp.headers["location"].split("/referrals/")[1])
        scope = Scope(user_id=user_id)
        referral = storage.get_referral(scope, referral_id)
        assert referral is not None
        assert referral.ehr_service_request_id == "SR-001"
    finally:
        app.dependency_overrides.clear()
        from docstats.ehr import epic
        epic._DISCOVERY_CACHE.clear()


def test_ehr_hook_service_request_failure_audits_and_referral_survives(tmp_path, monkeypatch):
    """ServiceRequest write failure → referral created; audit ehr.service_request_write_failed."""
    from docstats.ehr.epic import EpicError
    import docstats.ehr.epic as _epic

    client, storage, user_id, patient = _ehr_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(_epic, "fetch_conditions", lambda **_kw: [])
    monkeypatch.setattr(_epic, "fetch_medications", lambda **_kw: [])
    monkeypatch.setattr(_epic, "fetch_allergies", lambda **_kw: [])
    monkeypatch.setattr(_epic, "fetch_document_references", lambda **_kw: [])

    def _fail_sr(**_kw):
        raise EpicError("write_failed")

    monkeypatch.setattr(_epic, "write_service_request", _fail_sr)
    try:
        resp = _post_referral(client, patient.id)
        assert resp.status_code == 303
        referral_id = int(resp.headers["location"].split("/referrals/")[1])
        scope = Scope(user_id=user_id)
        referral = storage.get_referral(scope, referral_id)
        assert referral is not None
        assert referral.ehr_service_request_id is None
        events = storage.list_audit_events(actor_user_id=user_id, limit=20)
        assert any(e.action == "ehr.service_request_write_failed" for e in events)
    finally:
        app.dependency_overrides.clear()
        _epic._DISCOVERY_CACHE.clear()


def test_ehr_hook_doc_content_upload_stores_storage_ref(tmp_path, monkeypatch):
    """With ATTACHMENT_UPLOAD_ENABLED, doc content bytes are stored via file backend."""
    import base64
    import docstats.ehr.epic as _epic
    import docstats.storage_files.mime as _mime_mod
    from docstats.storage_files.factory import reset_memory_singleton_for_tests

    # A minimal valid PDF magic bytes.
    pdf_bytes = b"%PDF-1.4 fake-pdf-content"

    DOC_REF = {
        "resourceType": "DocumentReference",
        "type": {"text": "Progress Note"},
        "date": "2024-03-15T10:00:00Z",
        "content": [
            {
                "attachment": {
                    "data": base64.b64encode(pdf_bytes).decode(),
                    "contentType": "application/pdf",
                }
            }
        ],
    }

    client, storage, user_id, patient = _ehr_fixture(tmp_path, monkeypatch)
    monkeypatch.setenv("ATTACHMENT_UPLOAD_ENABLED", "1")
    monkeypatch.setenv("ATTACHMENT_STORAGE_BACKEND", "memory")
    monkeypatch.setattr(_epic, "fetch_conditions", lambda **_kw: [])
    monkeypatch.setattr(_epic, "fetch_medications", lambda **_kw: [])
    monkeypatch.setattr(_epic, "fetch_allergies", lambda **_kw: [])
    monkeypatch.setattr(_epic, "fetch_document_references", lambda **_kw: [DOC_REF])
    monkeypatch.setattr(_epic, "write_service_request", lambda **_kw: "SR-DOC")

    # Bypass real magic-byte check for our fake PDF.
    monkeypatch.setattr(_mime_mod, "sniff_mime", lambda _b: "application/pdf")

    reset_memory_singleton_for_tests()
    try:
        resp = _post_referral(client, patient.id)
        assert resp.status_code == 303
        referral_id = int(resp.headers["location"].split("/referrals/")[1])
        scope = Scope(user_id=user_id)
        attachments = storage.list_referral_attachments(scope, referral_id)
        uploaded = [a for a in attachments if a.storage_ref is not None]
        assert len(uploaded) >= 1
        assert uploaded[0].checklist_only is False
    finally:
        app.dependency_overrides.clear()
        _epic._DISCOVERY_CACHE.clear()
        reset_memory_singleton_for_tests()
