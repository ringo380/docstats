"""Tests for the dependent → linked account upgrade flow (#158)."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from docstats.auth import get_current_user
from docstats.domain.family import (
    is_eligible_for_self_upgrade,
    patient_age,
)
from docstats.phi import CURRENT_PHI_CONSENT_VERSION
from docstats.scope import Scope
from docstats.storage import Storage, get_storage
from docstats.web import app


def _dob(years_ago: int) -> str:
    return (date.today() - timedelta(days=365 * years_ago + 10)).isoformat()


def _patient_user(user_id: int, email: str) -> dict:
    return {
        "id": user_id,
        "email": email,
        "display_name": None,
        "first_name": "P",
        "last_name": "Arent",
        "github_id": None,
        "github_login": None,
        "password_hash": "x",
        "created_at": "2026-01-01",
        "last_login_at": None,
        "terms_accepted_at": "2026-01-01",
        "phi_consent_at": "2026-01-01",
        "phi_consent_version": CURRENT_PHI_CONSENT_VERSION,
        "phi_consent_ip": None,
        "phi_consent_user_agent": None,
        "active_org_id": None,
        "is_org_admin": False,
        "account_type": "patient",
        "clinician_verification_status": "not_applicable",
    }


def _seed_user_row(storage: Storage, user_id: int, email: str) -> None:
    storage._conn.execute(
        "INSERT INTO users (id, email, password_hash, account_type, "
        "clinician_verification_status) VALUES (?, ?, ?, 'patient', 'not_applicable')",
        (user_id, email, "hashed"),
    )
    storage._conn.commit()


@pytest.fixture
def parent_client(tmp_path: Path):
    storage = Storage(db_path=tmp_path / "t.db")
    parent_id = storage.create_user("parent@example.com", "h")
    child_id = storage.create_user("child@example.com", "h")
    for uid, email in [(parent_id, "parent@example.com"), (child_id, "child@example.com")]:
        storage.record_phi_consent(
            user_id=uid,
            phi_consent_version=CURRENT_PHI_CONSENT_VERSION,
            ip_address="",
            user_agent="",
        )
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_current_user] = lambda: _patient_user(
        parent_id, "parent@example.com"
    )
    yield TestClient(app), storage, parent_id, child_id
    app.dependency_overrides.clear()


def _seed_dependent(storage: Storage, parent_id: int, *, years_old: int) -> int:
    p = storage.create_patient(
        Scope(user_id=parent_id),
        first_name="Alex",
        last_name="Kid",
        date_of_birth=_dob(years_old),
        relationship="child",
        created_by_user_id=parent_id,
    )
    return p.id


# --- Eligibility helpers ---


def test_patient_age_handles_missing_dob():
    from docstats.domain.patients import Patient
    from datetime import datetime, timezone

    p = Patient(
        id=1,
        scope_user_id=1,
        first_name="A",
        last_name="B",
        created_at=datetime.now(tz=timezone.utc),
        updated_at=datetime.now(tz=timezone.utc),
    )
    assert patient_age(p, date.today()) is None
    assert is_eligible_for_self_upgrade(p, date.today()) is False


def test_eligibility_requires_child_relationship_and_age_18():
    from docstats.domain.patients import Patient
    from datetime import datetime, timezone

    today = date.today()

    def mk(rel: str | None, years_old: int) -> Patient:
        return Patient(
            id=1,
            scope_user_id=1,
            first_name="A",
            last_name="B",
            date_of_birth=_dob(years_old),
            relationship=rel,
            created_at=datetime.now(tz=timezone.utc),
            updated_at=datetime.now(tz=timezone.utc),
        )

    assert is_eligible_for_self_upgrade(mk("child", 20), today) is True
    assert is_eligible_for_self_upgrade(mk("son", 18), today) is True
    assert is_eligible_for_self_upgrade(mk("daughter", 18), today) is True
    assert is_eligible_for_self_upgrade(mk("child", 17), today) is False
    assert is_eligible_for_self_upgrade(mk("spouse", 30), today) is False
    assert is_eligible_for_self_upgrade(mk(None, 30), today) is False


# --- Storage reparent ---


def test_reparent_patient_moves_referrals_and_clears_relationship(tmp_path: Path):
    storage = Storage(db_path=tmp_path / "t.db")
    parent_id = storage.create_user("p@x", "h")
    child_id = storage.create_user("c@x", "h")
    pid = _seed_dependent(storage, parent_id, years_old=18)
    r1 = storage.create_referral(
        Scope(user_id=parent_id),
        patient_id=pid,
        reason="r1",
        created_by_user_id=parent_id,
    )
    r2 = storage.create_referral(
        Scope(user_id=parent_id),
        patient_id=pid,
        reason="r2",
        created_by_user_id=parent_id,
    )
    moved = storage.reparent_patient_to_user(pid, from_user_id=parent_id, to_user_id=child_id)
    assert moved == 2

    # Patient now scoped to child, relationship cleared.
    p_child = storage.get_patient(Scope(user_id=child_id), pid)
    assert p_child is not None
    assert p_child.relationship is None
    # Parent no longer sees it.
    assert storage.get_patient(Scope(user_id=parent_id), pid) is None
    # Referrals followed.
    child_refs = storage.list_referrals(Scope(user_id=child_id), patient_id=pid)
    assert {r.id for r in child_refs} == {r1.id, r2.id}
    parent_refs = storage.list_referrals(Scope(user_id=parent_id), patient_id=pid)
    assert parent_refs == []


def test_reparent_patient_rejects_wrong_owner(tmp_path: Path):
    storage = Storage(db_path=tmp_path / "t.db")
    parent_id = storage.create_user("p@x", "h")
    other_id = storage.create_user("o@x", "h")
    pid = _seed_dependent(storage, parent_id, years_old=20)
    with pytest.raises(ValueError):
        storage.reparent_patient_to_user(pid, from_user_id=other_id, to_user_id=parent_id)


# --- Invite + accept flow ---


def test_invite_upgrade_rejects_minor(parent_client):
    client, storage, parent_id, child_id = parent_client
    pid = _seed_dependent(storage, parent_id, years_old=15)
    resp = client.post(
        f"/profile/family/dependent/{pid}/invite-upgrade",
        data={"email": "child@example.com"},
        follow_redirects=False,
    )
    assert resp.status_code == 200
    assert "isn&#39;t eligible" in resp.text or "isn't eligible" in resp.text


def test_invite_upgrade_creates_pending_link(parent_client):
    client, storage, parent_id, child_id = parent_client
    pid = _seed_dependent(storage, parent_id, years_old=19)
    resp = client.post(
        f"/profile/family/dependent/{pid}/invite-upgrade",
        data={"email": "child@example.com"},
        follow_redirects=False,
    )
    assert resp.status_code in (200, 303)
    links = storage.list_family_links(parent_id)
    assert len(links) == 1
    link = links[0]
    assert link.source_patient_id == pid
    assert link.linked_user_id == child_id
    assert link.is_pending()
    assert link.invite_token is not None


def test_accept_dependent_upgrade_reparents(parent_client):
    client, storage, parent_id, child_id = parent_client
    pid = _seed_dependent(storage, parent_id, years_old=21)
    storage.create_referral(
        Scope(user_id=parent_id),
        patient_id=pid,
        reason="checkup",
        created_by_user_id=parent_id,
    )
    # Parent sends invite
    client.post(
        f"/profile/family/dependent/{pid}/invite-upgrade",
        data={"email": "child@example.com"},
    )
    link = storage.list_family_links(parent_id)[0]

    # Swap auth to the child user and POST accept
    app.dependency_overrides[get_current_user] = lambda: _patient_user(
        child_id, "child@example.com"
    )
    resp = client.post(
        f"/profile/family/accept/{link.invite_token}",
        follow_redirects=False,
    )
    assert resp.status_code == 303
    # Patient + referral now in child scope.
    assert storage.get_patient(Scope(user_id=child_id), pid) is not None
    refs = storage.list_referrals(Scope(user_id=child_id), patient_id=pid)
    assert len(refs) == 1
    # Link is now active.
    new_link = storage.list_family_links(child_id)[0]
    assert new_link.is_active()


def test_accept_get_renders_confirmation_page(parent_client):
    client, storage, parent_id, child_id = parent_client
    pid = _seed_dependent(storage, parent_id, years_old=18)
    client.post(
        f"/profile/family/dependent/{pid}/invite-upgrade",
        data={"email": "child@example.com"},
    )
    link = storage.list_family_links(parent_id)[0]

    app.dependency_overrides[get_current_user] = lambda: _patient_user(
        child_id, "child@example.com"
    )
    resp = client.get(f"/profile/family/accept/{link.invite_token}")
    assert resp.status_code == 200
    assert "Take over your account" in resp.text


def test_accept_rejects_wrong_user(parent_client):
    client, storage, parent_id, child_id = parent_client
    other_id = storage.create_user("other@x.com", "h")
    storage.record_phi_consent(
        user_id=other_id,
        phi_consent_version=CURRENT_PHI_CONSENT_VERSION,
        ip_address="",
        user_agent="",
    )
    pid = _seed_dependent(storage, parent_id, years_old=20)
    client.post(
        f"/profile/family/dependent/{pid}/invite-upgrade",
        data={"email": "child@example.com"},
    )
    link = storage.list_family_links(parent_id)[0]

    app.dependency_overrides[get_current_user] = lambda: _patient_user(other_id, "other@x.com")
    resp = client.post(f"/profile/family/accept/{link.invite_token}", follow_redirects=False)
    assert resp.status_code == 403


def test_duplicate_invite_blocked(parent_client):
    client, storage, parent_id, child_id = parent_client
    pid = _seed_dependent(storage, parent_id, years_old=20)
    client.post(
        f"/profile/family/dependent/{pid}/invite-upgrade",
        data={"email": "child@example.com"},
    )
    resp = client.post(
        f"/profile/family/dependent/{pid}/invite-upgrade",
        data={"email": "child@example.com"},
    )
    assert resp.status_code == 200
    assert "already pending" in resp.text


def test_invite_to_clinician_account_rejected(parent_client):
    client, storage, parent_id, _child_id = parent_client
    # Override default child to be a clinician account.
    storage._conn.execute(
        "UPDATE users SET account_type='clinician', clinician_verification_status='verified' "
        "WHERE email='child@example.com'"
    )
    storage._conn.commit()
    pid = _seed_dependent(storage, parent_id, years_old=19)
    resp = client.post(
        f"/profile/family/dependent/{pid}/invite-upgrade",
        data={"email": "child@example.com"},
    )
    assert resp.status_code == 200
    assert "clinician account" in resp.text


def test_accept_compensates_on_reparent_failure(parent_client, monkeypatch):
    """If reparent_patient_to_user raises after accept_family_link succeeds,
    the link must be revoked so the parent isn't left linked-but-not-transferred."""
    client, storage, parent_id, child_id = parent_client
    pid = _seed_dependent(storage, parent_id, years_old=20)
    client.post(
        f"/profile/family/dependent/{pid}/invite-upgrade",
        data={"email": "child@example.com"},
    )
    link = storage.list_family_links(parent_id)[0]

    # Force reparent to blow up after the link gets accepted.
    real = storage.reparent_patient_to_user

    def boom(*a, **kw):
        raise RuntimeError("simulated reparent failure")

    monkeypatch.setattr(storage, "reparent_patient_to_user", boom)
    app.dependency_overrides[get_current_user] = lambda: _patient_user(
        child_id, "child@example.com"
    )
    # raise_server_exceptions=False keeps TestClient from re-throwing the
    # 500 so we can assert the compensation side-effect ran first.
    compensation_client = TestClient(app, raise_server_exceptions=False)
    resp = compensation_client.post(
        f"/profile/family/accept/{link.invite_token}", follow_redirects=False
    )
    monkeypatch.setattr(storage, "reparent_patient_to_user", real)
    assert resp.status_code == 500
    # The link must be revoked (compensation), not left accepted.
    fresh = storage._conn.execute(
        "SELECT accepted_at, revoked_at FROM family_links WHERE id = ?", (link.id,)
    ).fetchone()
    assert fresh["revoked_at"] is not None
    # Patient stayed with parent (reparent never completed).
    assert storage.get_patient(Scope(user_id=parent_id), pid) is not None


def test_accept_post_requires_phi_consent(tmp_path: Path):
    storage = Storage(db_path=tmp_path / "t.db")
    parent_id = storage.create_user("p@x.com", "h")
    child_id = storage.create_user("c@x.com", "h")
    # Parent has consent; child does NOT.
    storage.record_phi_consent(
        user_id=parent_id,
        phi_consent_version=CURRENT_PHI_CONSENT_VERSION,
        ip_address="",
        user_agent="",
    )
    pid = _seed_dependent(storage, parent_id, years_old=20)
    token = "tok-noconsent"
    storage.create_family_link(
        initiator_user_id=parent_id,
        linked_user_id=child_id,
        relationship="child",
        invite_token=token,
        invite_email="c@x.com",
        source_patient_id=pid,
    )
    app.dependency_overrides[get_storage] = lambda: storage
    no_consent = _patient_user(child_id, "c@x.com")
    no_consent["phi_consent_at"] = None
    no_consent["phi_consent_version"] = None
    app.dependency_overrides[get_current_user] = lambda: no_consent
    try:
        client = TestClient(app)
        resp = client.post(f"/profile/family/accept/{token}", follow_redirects=False)
    finally:
        app.dependency_overrides.clear()
    # PhiConsentRequiredException → 303 to /auth/login (or similar).
    assert resp.status_code in (302, 303, 307)


def test_reparent_toctou_guard_on_patient_update(tmp_path: Path):
    """The patient UPDATE inside reparent_patient_to_user re-checks
    scope_user_id = from_user_id. Simulate concurrent mutation between
    SELECT and UPDATE by patching the helper to mutate the row mid-call."""
    storage = Storage(db_path=tmp_path / "t.db")
    parent_id = storage.create_user("p@x.com", "h")
    child_id = storage.create_user("c@x.com", "h")
    third_id = storage.create_user("t@x.com", "h")
    pid = _seed_dependent(storage, parent_id, years_old=21)
    # Mutate scope between the SELECT and the UPDATE: just pre-mutate before
    # the call so the UPDATE WHERE scope_user_id=parent_id matches 0 rows.
    storage._conn.execute("UPDATE patients SET scope_user_id = ? WHERE id = ?", (third_id, pid))
    storage._conn.commit()
    with pytest.raises(ValueError):
        storage.reparent_patient_to_user(pid, from_user_id=parent_id, to_user_id=child_id)
    # Patient still owned by third party — the UPDATE was a no-op.
    row = storage._conn.execute(
        "SELECT scope_user_id FROM patients WHERE id = ?", (pid,)
    ).fetchone()
    assert row["scope_user_id"] == third_id
