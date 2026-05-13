"""Tests for family insurance plan sharing (#159)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from docstats.auth import get_current_user
from docstats.phi import CURRENT_PHI_CONSENT_VERSION
from docstats.scope import Scope
from docstats.storage import Storage, get_storage
from docstats.web import app


def _patient_user(user_id: int, email: str) -> dict:
    return {
        "id": user_id,
        "email": email,
        "display_name": None,
        "first_name": "Pat",
        "last_name": "User",
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


def _link_users(storage: Storage, a_id: int, b_id: int) -> None:
    storage.create_family_link(
        initiator_user_id=a_id,
        linked_user_id=b_id,
        relationship="spouse",
        invite_token="tok-link",
        invite_email="b@x.com",
    )
    links = storage.list_family_links(a_id)
    storage.accept_family_link(links[0].id, b_id)


@pytest.fixture
def holder_client(tmp_path: Path):
    storage = Storage(db_path=tmp_path / "s.db")
    holder_id = storage.create_user("holder@x.com", "h")
    for uid in (holder_id,):
        storage.record_phi_consent(
            user_id=uid,
            phi_consent_version=CURRENT_PHI_CONSENT_VERSION,
            ip_address="",
            user_agent="",
        )
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_current_user] = lambda: _patient_user(holder_id, "holder@x.com")
    yield TestClient(app), storage, holder_id
    app.dependency_overrides.clear()


# --- Storage: share toggle + cross-scope visibility ---


def test_set_insurance_plan_share_toggles_flag(holder_client):
    _, storage, holder_id = holder_client
    plan = storage.create_insurance_plan(
        Scope(user_id=holder_id), payer_name="Acme Health", plan_type="ppo"
    )
    assert plan.shared_with_family is False
    out = storage.set_insurance_plan_share(Scope(user_id=holder_id), plan.id, shared=True)
    assert out is not None and out.shared_with_family is True
    out2 = storage.set_insurance_plan_share(Scope(user_id=holder_id), plan.id, shared=False)
    assert out2 is not None and out2.shared_with_family is False


def test_set_share_rejects_other_users_plan(holder_client):
    _, storage, holder_id = holder_client
    other_id = storage.create_user("other@x.com", "h")
    plan = storage.create_insurance_plan(
        Scope(user_id=other_id), payer_name="Other", plan_type="hmo"
    )
    out = storage.set_insurance_plan_share(Scope(user_id=holder_id), plan.id, shared=True)
    assert out is None


def test_shared_plan_visible_to_linked_family(tmp_path: Path):
    storage = Storage(db_path=tmp_path / "s.db")
    holder_id = storage.create_user("h@x.com", "x")
    spouse_id = storage.create_user("s@x.com", "x")
    plan = storage.create_insurance_plan(
        Scope(user_id=holder_id), payer_name="Blue Sky", plan_type="ppo"
    )
    # Without sharing or link, spouse sees nothing.
    assert storage.list_shared_family_plans(spouse_id) == []
    storage.set_insurance_plan_share(Scope(user_id=holder_id), plan.id, shared=True)
    # Sharing alone isn't enough — link must exist.
    assert storage.list_shared_family_plans(spouse_id) == []
    _link_users(storage, holder_id, spouse_id)
    visible = storage.list_shared_family_plans(spouse_id)
    assert len(visible) == 1 and visible[0].id == plan.id


def test_unshared_plan_disappears_when_toggled_off(tmp_path: Path):
    storage = Storage(db_path=tmp_path / "s.db")
    holder_id = storage.create_user("h@x.com", "x")
    spouse_id = storage.create_user("s@x.com", "x")
    _link_users(storage, holder_id, spouse_id)
    plan = storage.create_insurance_plan(Scope(user_id=holder_id), payer_name="X", plan_type="ppo")
    storage.set_insurance_plan_share(Scope(user_id=holder_id), plan.id, shared=True)
    assert len(storage.list_shared_family_plans(spouse_id)) == 1
    storage.set_insurance_plan_share(Scope(user_id=holder_id), plan.id, shared=False)
    assert storage.list_shared_family_plans(spouse_id) == []


def test_pending_link_does_not_grant_visibility(tmp_path: Path):
    storage = Storage(db_path=tmp_path / "s.db")
    holder_id = storage.create_user("h@x.com", "x")
    spouse_id = storage.create_user("s@x.com", "x")
    storage.create_family_link(
        initiator_user_id=holder_id,
        linked_user_id=spouse_id,
        relationship="spouse",
        invite_token="tk",
        invite_email="s@x.com",
    )
    # Not accepted yet
    plan = storage.create_insurance_plan(Scope(user_id=holder_id), payer_name="P", plan_type="ppo")
    storage.set_insurance_plan_share(Scope(user_id=holder_id), plan.id, shared=True)
    assert storage.list_shared_family_plans(spouse_id) == []


# --- Routes: share toggle + plan list ---


def test_insurance_list_page_renders(holder_client):
    client, storage, holder_id = holder_client
    storage.create_insurance_plan(Scope(user_id=holder_id), payer_name="Aetna", plan_type="ppo")
    resp = client.get("/profile/insurance")
    assert resp.status_code == 200
    assert "Aetna" in resp.text


def test_share_toggle_via_route(holder_client):
    client, storage, holder_id = holder_client
    plan = storage.create_insurance_plan(
        Scope(user_id=holder_id), payer_name="Aetna", plan_type="ppo"
    )
    resp = client.post(
        f"/profile/insurance/{plan.id}/share",
        data={"shared": "1"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    refreshed = storage.get_insurance_plan(Scope(user_id=holder_id), plan.id)
    assert refreshed is not None and refreshed.shared_with_family is True


def test_create_plan_via_route(holder_client):
    client, storage, holder_id = holder_client
    resp = client.post(
        "/profile/insurance",
        data={"payer_name": "Kaiser", "plan_type": "hmo"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    plans = storage.list_insurance_plans(Scope(user_id=holder_id))
    assert len(plans) == 1 and plans[0].payer_name == "Kaiser"


def test_pipe_in_payer_name_rejected(holder_client):
    client, _, _ = holder_client
    resp = client.post(
        "/profile/insurance",
        data={"payer_name": "Bad|Name", "plan_type": "ppo"},
    )
    assert resp.status_code == 200
    assert "must not contain" in resp.text


# --- Picker integration: shared plan clones on selection ---


def test_referral_picker_clones_shared_plan(tmp_path: Path):
    storage = Storage(db_path=tmp_path / "s.db")
    holder_id = storage.create_user("h@x.com", "x")
    spouse_id = storage.create_user("s@x.com", "x")
    for uid in (holder_id, spouse_id):
        storage.record_phi_consent(
            user_id=uid,
            phi_consent_version=CURRENT_PHI_CONSENT_VERSION,
            ip_address="",
            user_agent="",
        )
    _link_users(storage, holder_id, spouse_id)
    plan = storage.create_insurance_plan(
        Scope(user_id=holder_id), payer_name="Shared", plan_type="ppo"
    )
    storage.set_insurance_plan_share(Scope(user_id=holder_id), plan.id, shared=True)
    # Spouse adds their own patient + posts a referral that picks the shared plan.
    pt = storage.create_patient(
        Scope(user_id=spouse_id),
        first_name="Self",
        last_name="Pt",
        created_by_user_id=spouse_id,
    )
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_current_user] = lambda: _patient_user(spouse_id, "s@x.com")
    try:
        client = TestClient(app)
        resp = client.post(
            "/referrals",
            data={
                "patient_id": str(pt.id),
                "reason": "consult",
                "urgency": "routine",
                "payer_plan_id": str(plan.id),
            },
            follow_redirects=False,
        )
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code in (200, 303)
    # The referral should have a payer_plan_id that points to a CLONE in spouse's scope.
    refs = storage.list_referrals(Scope(user_id=spouse_id), patient_id=pt.id)
    assert len(refs) == 1
    cloned_id = refs[0].payer_plan_id
    assert cloned_id is not None
    cloned = storage.get_insurance_plan(Scope(user_id=spouse_id), cloned_id)
    assert cloned is not None
    assert cloned.id != plan.id
    assert cloned.payer_name == "Shared"


def test_clone_dedupes_on_repeat_selection(tmp_path: Path):
    storage = Storage(db_path=tmp_path / "s.db")
    holder_id = storage.create_user("h@x.com", "x")
    spouse_id = storage.create_user("s@x.com", "x")
    for uid in (holder_id, spouse_id):
        storage.record_phi_consent(
            user_id=uid,
            phi_consent_version=CURRENT_PHI_CONSENT_VERSION,
            ip_address="",
            user_agent="",
        )
    _link_users(storage, holder_id, spouse_id)
    plan = storage.create_insurance_plan(
        Scope(user_id=holder_id), payer_name="Dedupe", plan_type="ppo"
    )
    storage.set_insurance_plan_share(Scope(user_id=holder_id), plan.id, shared=True)
    pt = storage.create_patient(
        Scope(user_id=spouse_id),
        first_name="A",
        last_name="B",
        created_by_user_id=spouse_id,
    )
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_current_user] = lambda: _patient_user(spouse_id, "s@x.com")
    try:
        client = TestClient(app)
        for _ in range(3):
            client.post(
                "/referrals",
                data={
                    "patient_id": str(pt.id),
                    "reason": "consult",
                    "urgency": "routine",
                    "payer_plan_id": str(plan.id),
                },
                follow_redirects=False,
            )
    finally:
        app.dependency_overrides.clear()
    # 3 referrals, but only 1 local clone (dedup by payer_name+plan_type+plan_name).
    local = storage.list_insurance_plans(Scope(user_id=spouse_id))
    assert len(local) == 1
    refs = storage.list_referrals(Scope(user_id=spouse_id), patient_id=pt.id)
    assert len(refs) == 3
    assert {r.payer_plan_id for r in refs} == {local[0].id}
