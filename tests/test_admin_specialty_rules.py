"""Route-level tests for admin specialty-rules editor (Phase 6.B)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from docstats.auth import get_current_user
from docstats.storage import Storage, get_storage
from docstats.web import app


def _fake_user(
    user_id: int,
    email: str,
    *,
    active_org_id: int | None = None,
    is_org_admin: bool = False,
) -> dict:
    return {
        "id": user_id,
        "email": email,
        "display_name": None,
        "first_name": None,
        "last_name": None,
        "github_id": None,
        "github_login": None,
        "password_hash": "hashed",
        "created_at": "2026-01-01",
        "last_login_at": None,
        "terms_accepted_at": "2026-01-01",
        "active_org_id": active_org_id,
        "is_org_admin": is_org_admin,
    }


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    return Storage(db_path=tmp_path / "admin_specialty.db")


@pytest.fixture
def org_admin(storage: Storage):
    """Set up an org with a single admin user + one global specialty rule."""
    user_id = storage.create_user("admin@example.com", "hashed")
    org = storage.create_organization(name="Acme Cardiology", slug="acme-cardio")
    storage.create_membership(organization_id=org.id, user_id=user_id, role="admin")
    storage.set_active_org(user_id, org.id)
    # Seed a global Cardiology rule to exercise the override workflow.
    storage.create_specialty_rule(
        specialty_code="207RC0000X",
        organization_id=None,
        display_name="Cardiology",
        required_fields={"fields": ["reason", "clinical_question"]},
        recommended_attachments={
            "kinds": ["lab", "imaging"],
            "labels": ["Recent EKG", "Lipid panel"],
        },
        intake_questions={"prompts": ["Duration of symptoms?"]},
        urgency_red_flags={"keywords": ["chest pain", "syncope"]},
        common_rejection_reasons={"reasons": ["Missing recent EKG"]},
        source="seed",
    )
    user = _fake_user(user_id, "admin@example.com", active_org_id=org.id, is_org_admin=True)
    return user_id, org, user


def _client_with(storage: Storage, user: dict | None) -> TestClient:
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


def _cleanup() -> None:
    app.dependency_overrides.clear()


# --- Role enforcement (mirrors 6.A coverage for the new surface) ---


def test_list_rejects_solo_user(storage: Storage) -> None:
    user_id = storage.create_user("solo@example.com", "hashed")
    user = _fake_user(user_id, "solo@example.com", active_org_id=None)
    try:
        resp = _client_with(storage, user).get("/admin/specialty-rules")
        assert resp.status_code == 403
    finally:
        _cleanup()


@pytest.mark.parametrize("role", ["read_only", "staff", "clinician", "coordinator"])
def test_list_rejects_sub_admin(storage: Storage, role: str) -> None:
    user_id = storage.create_user(f"{role}@example.com", "hashed")
    org = storage.create_organization(name="R", slug=f"r-{role}")
    storage.create_membership(organization_id=org.id, user_id=user_id, role=role)
    storage.set_active_org(user_id, org.id)
    user = _fake_user(user_id, f"{role}@example.com", active_org_id=org.id, is_org_admin=False)
    try:
        resp = _client_with(storage, user).get("/admin/specialty-rules")
        assert resp.status_code == 403
    finally:
        _cleanup()


def test_edit_rejects_anonymous(storage: Storage) -> None:
    try:
        resp = _client_with(storage, None).get(
            "/admin/specialty-rules/207RC0000X", follow_redirects=False
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/auth/login"
    finally:
        _cleanup()


def test_save_rejects_non_admin(storage: Storage) -> None:
    user_id = storage.create_user("staff@example.com", "hashed")
    org = storage.create_organization(name="S", slug="s-org")
    storage.create_membership(organization_id=org.id, user_id=user_id, role="staff")
    storage.set_active_org(user_id, org.id)
    user = _fake_user(user_id, "staff@example.com", active_org_id=org.id, is_org_admin=False)
    try:
        resp = _client_with(storage, user).post(
            "/admin/specialty-rules/207RC0000X", data={"display_name": "X"}
        )
        assert resp.status_code == 403
    finally:
        _cleanup()


# --- List view ---


def test_list_shows_globals_with_no_override(storage: Storage, org_admin) -> None:
    _, _, user = org_admin
    try:
        resp = _client_with(storage, user).get("/admin/specialty-rules")
        assert resp.status_code == 200
        body = resp.text
        assert "Cardiology" in body
        assert "207RC0000X" in body
        assert "Platform default" in body
        # The "Edit override" button reads "Create override" when no override exists yet.
        assert "Create override" in body
        assert "Revert" not in body
    finally:
        _cleanup()


def test_list_highlights_existing_override(storage: Storage, org_admin) -> None:
    _, org, user = org_admin
    storage.create_specialty_rule(
        specialty_code="207RC0000X",
        organization_id=org.id,
        display_name="Cardiology (org)",
        required_fields={"fields": ["reason"]},
        source="admin_override",
    )
    try:
        resp = _client_with(storage, user).get("/admin/specialty-rules")
        assert resp.status_code == 200
        body = resp.text
        assert "Org override" in body
        # Override display_name wins over global.
        assert "Cardiology (org)" in body
        assert "Edit override" in body
        assert "Revert" in body
    finally:
        _cleanup()


# --- Edit form GET ---


def test_edit_form_prepopulates_from_global_when_no_override(storage: Storage, org_admin) -> None:
    _, _, user = org_admin
    try:
        resp = _client_with(storage, user).get("/admin/specialty-rules/207RC0000X")
        assert resp.status_code == 200
        body = resp.text
        # Values seeded from the global rule.
        assert 'value="Cardiology"' in body
        assert "Recent EKG" in body
        assert "Lipid panel" in body
        assert "chest pain" in body
        assert "Missing recent EKG" in body
        # Required-field checkboxes: the two from global should be checked.
        assert 'name="required_field" value="reason"' in body
        assert 'name="required_field" value="clinical_question"' in body
        # Button label reflects "Create override" flow.
        assert "Create override" in body
    finally:
        _cleanup()


def test_edit_form_prepopulates_from_override_when_present(storage: Storage, org_admin) -> None:
    _, org, user = org_admin
    storage.create_specialty_rule(
        specialty_code="207RC0000X",
        organization_id=org.id,
        display_name="Cardiology — Internal Med heavy",
        required_fields={"fields": ["reason"]},
        recommended_attachments={"kinds": [], "labels": ["Custom label"]},
        intake_questions={"prompts": ["Custom prompt?"]},
        urgency_red_flags={"keywords": ["org-keyword"]},
        common_rejection_reasons={"reasons": ["org-reason"]},
        source="admin_override",
    )
    try:
        resp = _client_with(storage, user).get("/admin/specialty-rules/207RC0000X")
        assert resp.status_code == 200
        body = resp.text
        assert "Cardiology — Internal Med heavy" in body
        assert "Custom label" in body
        assert "Custom prompt?" in body
        assert "org-keyword" in body
        assert "org-reason" in body
        # Global-only values should NOT appear when an override is seeding the
        # form. "Lipid panel" is a global-seeded textarea value; it would only
        # appear in the form if the override-seeding path leaked global data.
        # (The help-text "Recent EKG" example is a literal in the template,
        # so we assert on a global-only value that doesn't appear in help text.)
        assert "Lipid panel" not in body
        assert "Save override" in body
    finally:
        _cleanup()


def test_edit_form_404_when_neither_global_nor_override_exists(storage: Storage, org_admin) -> None:
    _, _, user = org_admin
    try:
        resp = _client_with(storage, user).get("/admin/specialty-rules/999BOGUSX")
        assert resp.status_code == 404
    finally:
        _cleanup()


# --- Save (create override) ---


def test_save_creates_org_override(storage: Storage, org_admin) -> None:
    _, org, user = org_admin
    try:
        resp = _client_with(storage, user).post(
            "/admin/specialty-rules/207RC0000X",
            data={
                "display_name": "Cardiology — Team override",
                "required_field": ["reason", "diagnosis_primary_icd"],
                "recommended_attachment_labels": "EKG\nStress test",
                "intake_question_prompts": "How long?\nAny syncope?",
                "urgency_red_flag_keywords": "chest pain\ndyspnea at rest",
                "common_rejection_reasons": "No EKG",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/admin/specialty-rules"

        overrides = storage.list_specialty_rules(organization_id=org.id, include_globals=False)
        assert len(overrides) == 1
        o = overrides[0]
        assert o.display_name == "Cardiology — Team override"
        assert o.required_fields == {"fields": ["reason", "diagnosis_primary_icd"]}
        assert o.recommended_attachments["labels"] == ["EKG", "Stress test"]
        # Kinds preserved from the seeding source (the global rule in this test).
        assert o.recommended_attachments["kinds"] == ["lab", "imaging"]
        assert o.intake_questions == {"prompts": ["How long?", "Any syncope?"]}
        assert o.urgency_red_flags == {"keywords": ["chest pain", "dyspnea at rest"]}
        assert o.common_rejection_reasons == {"reasons": ["No EKG"]}
        assert o.source == "admin_override"

        events = storage.list_audit_events(scope_organization_id=org.id)
        actions = [e.action for e in events]
        assert "admin.specialty_rule.create_override" in actions
    finally:
        _cleanup()


def test_save_filters_unknown_required_fields(storage: Storage, org_admin) -> None:
    _, org, user = org_admin
    try:
        resp = _client_with(storage, user).post(
            "/admin/specialty-rules/207RC0000X",
            data={
                "display_name": "Cardiology",
                # First is valid, second is not in _REQUIRED_FIELD_CHECKS and
                # should be silently dropped at the route boundary.
                "required_field": ["reason", "bogus_field"],
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        overrides = storage.list_specialty_rules(organization_id=org.id, include_globals=False)
        assert overrides[0].required_fields == {"fields": ["reason"]}
    finally:
        _cleanup()


def test_save_updates_existing_override(storage: Storage, org_admin) -> None:
    _, org, user = org_admin
    # Pre-existing override the admin is editing.
    before = storage.create_specialty_rule(
        specialty_code="207RC0000X",
        organization_id=org.id,
        display_name="Old name",
        required_fields={"fields": ["reason"]},
        recommended_attachments={"kinds": ["keep"], "labels": ["Old label"]},
        source="admin_override",
    )
    try:
        resp = _client_with(storage, user).post(
            "/admin/specialty-rules/207RC0000X",
            data={
                "display_name": "New name",
                "required_field": ["reason", "clinical_question"],
                "recommended_attachment_labels": "New label",
                "intake_question_prompts": "",
                "urgency_red_flag_keywords": "",
                "common_rejection_reasons": "",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303

        after = storage.get_specialty_rule(before.id)
        assert after is not None
        assert after.display_name == "New name"
        assert after.required_fields == {"fields": ["reason", "clinical_question"]}
        assert after.recommended_attachments["labels"] == ["New label"]
        # Kinds preserved from the override's own prior state, not re-seeded from global.
        assert after.recommended_attachments["kinds"] == ["keep"]
        # Empty textareas land as empty lists (not None).
        assert after.intake_questions == {"prompts": []}
        assert after.urgency_red_flags == {"keywords": []}
        assert after.common_rejection_reasons == {"reasons": []}
        # version_id bumped (was 1 on create, should now be 2).
        assert after.version_id > before.version_id

        events = storage.list_audit_events(scope_organization_id=org.id)
        assert any(e.action == "admin.specialty_rule.update_override" for e in events)
    finally:
        _cleanup()


def test_save_404_on_unknown_code(storage: Storage, org_admin) -> None:
    _, _, user = org_admin
    try:
        resp = _client_with(storage, user).post(
            "/admin/specialty-rules/999BOGUSX",
            data={"display_name": "Nope"},
        )
        assert resp.status_code == 404
    finally:
        _cleanup()


# --- Revert ---


def test_revert_deletes_override(storage: Storage, org_admin) -> None:
    _, org, user = org_admin
    storage.create_specialty_rule(
        specialty_code="207RC0000X",
        organization_id=org.id,
        display_name="Override to revert",
        source="admin_override",
    )
    assert len(storage.list_specialty_rules(organization_id=org.id, include_globals=False)) == 1
    try:
        resp = _client_with(storage, user).post(
            "/admin/specialty-rules/207RC0000X/revert",
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert len(storage.list_specialty_rules(organization_id=org.id, include_globals=False)) == 0
        # Global still present.
        globals_ = storage.list_specialty_rules(
            organization_id=None, include_globals=True, specialty_code="207RC0000X"
        )
        assert len(globals_) == 1

        events = storage.list_audit_events(scope_organization_id=org.id)
        assert any(e.action == "admin.specialty_rule.revert_override" for e in events)
    finally:
        _cleanup()


def test_revert_is_idempotent_when_no_override(storage: Storage, org_admin) -> None:
    _, org, user = org_admin
    try:
        resp = _client_with(storage, user).post(
            "/admin/specialty-rules/207RC0000X/revert",
            follow_redirects=False,
        )
        # No-op but still a redirect; should NOT emit an audit event.
        assert resp.status_code == 303
        events = storage.list_audit_events(scope_organization_id=org.id)
        assert all(e.action != "admin.specialty_rule.revert_override" for e in events)
    finally:
        _cleanup()


# --- Regression: HX-Request honored on every revert exit path ---


def test_revert_returns_hx_redirect_for_htmx(storage: Storage, org_admin) -> None:
    """Htmx callers must get ``HX-Redirect`` (200), not a 303 that htmx
    silently ignores. Covers all three revert exit paths (success, idempotent
    no-op, TOCTOU fallback) via the shared ``_redirect_after_save`` helper."""
    _, org, user = org_admin
    storage.create_specialty_rule(
        specialty_code="207RC0000X",
        organization_id=org.id,
        display_name="To revert",
        source="admin_override",
    )
    try:
        resp = _client_with(storage, user).post(
            "/admin/specialty-rules/207RC0000X/revert",
            headers={"HX-Request": "true"},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert resp.headers.get("hx-redirect") == "/admin/specialty-rules"
    finally:
        _cleanup()


def test_revert_idempotent_hx_redirect(storage: Storage, org_admin) -> None:
    """Idempotent no-op branch (no override present) also honors HX-Request."""
    _, _, user = org_admin
    try:
        resp = _client_with(storage, user).post(
            "/admin/specialty-rules/207RC0000X/revert",
            headers={"HX-Request": "true"},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert resp.headers.get("hx-redirect") == "/admin/specialty-rules"
    finally:
        _cleanup()


def test_save_success_hx_redirect(storage: Storage, org_admin) -> None:
    """Save's success path also goes through ``_redirect_after_save``; verify
    the htmx contract holds end-to-end."""
    _, _, user = org_admin
    try:
        resp = _client_with(storage, user).post(
            "/admin/specialty-rules/207RC0000X",
            data={"display_name": "OK"},
            headers={"HX-Request": "true"},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert resp.headers.get("hx-redirect") == "/admin/specialty-rules"
    finally:
        _cleanup()


# --- Regression: update path honors cleared display_name ---


def test_update_override_returns_404_when_row_vanishes_mid_flight(
    storage: Storage, org_admin
) -> None:
    """TOCTOU: the override is soft/hard-deleted between the route's
    guard read and the storage update. The update must surface a 404
    and NOT emit an audit event against the vanished row."""
    _, org, user = org_admin
    override = storage.create_specialty_rule(
        specialty_code="207RC0000X",
        organization_id=org.id,
        display_name="will vanish",
        source="admin_override",
    )
    # Simulate the race: monkey-patch update_specialty_rule to return None
    # (what storage does when the row has been deleted). Keep the original
    # for other callers.
    original = storage.update_specialty_rule
    calls = {"n": 0}

    def fake_update(*args, **kwargs):
        calls["n"] += 1
        return None

    storage.update_specialty_rule = fake_update  # type: ignore[method-assign]
    try:
        resp = _client_with(storage, user).post(
            "/admin/specialty-rules/207RC0000X",
            data={"display_name": "new"},
        )
        assert resp.status_code == 404
        events = storage.list_audit_events(scope_organization_id=org.id)
        assert all(e.action != "admin.specialty_rule.update_override" for e in events)
    finally:
        storage.update_specialty_rule = original  # type: ignore[method-assign]
        # Clean up the real row.
        storage.delete_specialty_rule(override.id)
        _cleanup()


def test_save_race_on_create_falls_through_to_update(storage: Storage, org_admin) -> None:
    """Concurrent create race: admin A and admin B both click "Create
    override" for the same specialty. A wins; B's insert hits the partial
    unique index and raises IntegrityError. The route must catch it,
    re-find the now-existing override, and route through update (treating
    the second admin's edits as a transparent update)."""
    _, org, user = org_admin

    # Pre-insert an override to simulate the race winner. The route's
    # _find_specialty_rule_for call at the top won't see it because we
    # monkey-patch that check to return None (simulating the pre-race
    # state), then let the real storage.create hit the unique-index
    # violation.
    winner = storage.create_specialty_rule(
        specialty_code="207RC0000X",
        organization_id=org.id,
        display_name="winner",
        source="admin_override",
    )

    # Patch _find_specialty_rule_for to return None on first call only
    # (pre-race state). The route also calls it inside the except block;
    # that call should return the real winner row.
    import docstats.routes.admin as admin_mod

    original_find = admin_mod._find_specialty_rule_for
    calls = {"n": 0}

    def fake_find(storage_arg, *, organization_id, specialty_code):
        calls["n"] += 1
        if calls["n"] == 1:
            # First call: pretend no override exists (pre-race).
            return None
        return original_find(
            storage_arg, organization_id=organization_id, specialty_code=specialty_code
        )

    admin_mod._find_specialty_rule_for = fake_find  # type: ignore[assignment]
    try:
        resp = _client_with(storage, user).post(
            "/admin/specialty-rules/207RC0000X",
            data={
                "display_name": "second admin edits",
                "required_field": ["reason"],
            },
            follow_redirects=False,
        )
        # Should succeed as a redirect, not 500.
        assert resp.status_code == 303
        # The winner row should now have the second admin's edits.
        row = storage.get_specialty_rule(winner.id)
        assert row is not None
        assert row.display_name == "second admin edits"
    finally:
        admin_mod._find_specialty_rule_for = original_find  # type: ignore[assignment]
        _cleanup()


def test_update_override_clears_display_name_when_field_emptied(
    storage: Storage, org_admin
) -> None:
    """Regression: admin clears the ``display_name`` input on an existing
    override; the route must honor the intent and write ``NULL`` rather than
    silently preserving the previous value.

    The storage contract is "None means leave unchanged" by default —
    ``update_specialty_rule`` must be called with ``overwrite=True`` so an
    empty form submission writes the None through.
    """
    _, org, user = org_admin
    storage.create_specialty_rule(
        specialty_code="207RC0000X",
        organization_id=org.id,
        display_name="Old override name",
        source="admin_override",
    )
    try:
        resp = _client_with(storage, user).post(
            "/admin/specialty-rules/207RC0000X",
            data={
                # display_name blank (form submitted empty) → should clear.
                "display_name": "",
                "required_field": ["reason"],
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303

        overrides = storage.list_specialty_rules(
            organization_id=org.id, include_globals=False, specialty_code="207RC0000X"
        )
        assert len(overrides) == 1
        assert overrides[0].display_name is None, (
            "Cleared display_name must be written through; the default "
            "overwrite=False would silently preserve the old value."
        )
    finally:
        _cleanup()
