"""Tests for web route behavior."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from docstats.web import app
from docstats.models import NPIResponse, NPIResult
from tests.conftest import SAMPLE_NPI1_RESULT


@pytest.fixture
def client(tmp_path: Path):
    """TestClient with storage, client, and auth dependencies overridden."""
    from docstats.storage import Storage, get_storage
    from docstats.web import get_client
    from docstats.auth import get_current_user

    storage = Storage(db_path=tmp_path / "test.db")
    user_id = storage.create_user("test@example.com", "hashed_pw")
    fake_user = {
        "id": user_id,
        "email": "test@example.com",
        "display_name": None,
        "github_id": None,
        "github_login": None,
        "password_hash": "hashed_pw",
        "created_at": "2026-01-01",
        "last_login_at": None,
    }
    mock_client = MagicMock()
    mock_client.async_search = AsyncMock()
    mock_client.async_lookup = AsyncMock()
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_client] = lambda: mock_client
    app.dependency_overrides[get_current_user] = lambda: fake_user
    yield TestClient(app), storage, mock_client, user_id
    app.dependency_overrides.clear()


def _make_response(results=None):
    results = results or [NPIResult.model_validate(SAMPLE_NPI1_RESULT)]
    return NPIResponse(result_count=len(results), results=results)


def test_search_with_query_param(client):
    test_client, storage, mock_client, user_id = client
    mock_client.async_search.return_value = _make_response()
    resp = test_client.get("/search", params={"query": "dr sarah chen"})
    assert resp.status_code == 200
    assert mock_client.async_search.called


def test_search_tries_interpretations_until_results(client):
    """If first interpretation returns empty, tries the next one."""
    test_client, storage, mock_client, user_id = client
    empty = NPIResponse(result_count=0, results=[])
    full = _make_response()
    mock_client.async_search.side_effect = [empty, full]
    resp = test_client.get("/search", params={"query": "dr kim do orthopedics"})
    assert resp.status_code == 200
    assert mock_client.async_search.call_count == 2


def test_search_returns_interp_desc(client):
    """Response HTML includes 'Searched as:' text."""
    test_client, storage, mock_client, user_id = client
    mock_client.async_search.return_value = _make_response()
    resp = test_client.get("/search", params={"query": "dr sarah chen cardiology"})
    assert "Searched as:" in resp.text


# ─────────────────────────────────────────────────────────────────
# Appointment-address wizard (replaces the prior set_appt_* /
# clear_appt_address / toggle_televisit / update_appt_contact routes).
# ─────────────────────────────────────────────────────────────────


def test_appt_wizard_open_returns_step1(client):
    """GET /provider/{npi}/appt-wizard renders the step-1 modal."""
    test_client, storage, mock_client, user_id = client
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    storage.save_provider(result, user_id)
    resp = test_client.get("/provider/1234567890/appt-wizard")
    assert resp.status_code == 200
    body = resp.text
    assert 'id="appt-wizard-modal"' in body
    assert 'name="visit_location_type"' in body
    assert 'value="practice"' in body
    assert 'value="televisit"' in body
    assert 'value="custom"' in body


def test_appt_wizard_open_unknown_provider_returns_error(client):
    """Unknown NPI returns a small error modal, not 500."""
    test_client, storage, mock_client, user_id = client
    resp = test_client.get("/provider/9999999999/appt-wizard")
    assert resp.status_code == 200
    assert "Save this provider first" in resp.text


def test_appt_wizard_close(client):
    """DELETE /provider/{npi}/appt-wizard returns empty body to clear modal-root."""
    test_client, storage, mock_client, user_id = client
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    storage.save_provider(result, user_id)
    resp = test_client.delete("/provider/1234567890/appt-wizard")
    assert resp.status_code == 200
    assert resp.text == ""


def test_appt_wizard_step1_practice_advances_to_step3(client):
    """Choosing 'practice' skips step 2 (no address needed)."""
    test_client, storage, mock_client, user_id = client
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    storage.save_provider(result, user_id)
    resp = test_client.post(
        "/provider/1234567890/appt-wizard",
        data={"step": "1", "visit_location_type": "practice"},
    )
    assert resp.status_code == 200
    # step-3 markers
    assert 'name="appt_phone"' in resp.text
    assert 'name="appt_fax"' in resp.text
    # No row write yet (finish happens at step 3 submit)
    provider = storage.get_provider("1234567890", user_id)
    assert provider.visit_location_type is None


def test_appt_wizard_step1_custom_advances_to_step2(client):
    """Choosing 'custom' shows the address step."""
    test_client, storage, mock_client, user_id = client
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    storage.save_provider(result, user_id)
    resp = test_client.post(
        "/provider/1234567890/appt-wizard",
        data={"step": "1", "visit_location_type": "custom"},
    )
    assert resp.status_code == 200
    # step-2 markers (address input present)
    assert "appt_address" in resp.text


def test_appt_wizard_step1_missing_choice_re_renders_with_error(client):
    """Submitting step 1 with no radio picked shows an inline error."""
    test_client, storage, mock_client, user_id = client
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    storage.save_provider(result, user_id)
    resp = test_client.post(
        "/provider/1234567890/appt-wizard",
        data={"step": "1", "visit_location_type": ""},
    )
    assert resp.status_code == 200
    assert "Pick how you visit" in resp.text


def test_appt_wizard_finish_practice(client):
    """Step 3 submit on 'practice' branch writes the row + clears appt fields."""
    test_client, storage, mock_client, user_id = client
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    storage.save_provider(result, user_id)
    resp = test_client.post(
        "/provider/1234567890/appt-wizard",
        data={
            "step": "3",
            "visit_location_type": "practice",
            "appt_phone": "(555) 123-4567",
            "appt_fax": "",
        },
    )
    assert resp.status_code == 200
    provider = storage.get_provider("1234567890", user_id)
    assert provider.visit_location_type == "practice"
    assert provider.is_televisit is False
    assert provider.appt_address is None
    assert provider.appt_suite is None
    assert provider.appt_phone == "(555) 123-4567"
    assert provider.appt_fax is None


def test_appt_wizard_finish_televisit_clears_address(client):
    """Telehealth branch never stores an appt_address."""
    test_client, storage, mock_client, user_id = client
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    storage.save_provider(result, user_id)
    # Pre-existing custom address must be cleared on switching to televisit.
    storage.set_visit_details(
        "1234567890",
        user_id,
        visit_location_type="custom",
        appt_address="123 Old St",
    )
    resp = test_client.post(
        "/provider/1234567890/appt-wizard",
        data={"step": "3", "visit_location_type": "televisit"},
    )
    assert resp.status_code == 200
    provider = storage.get_provider("1234567890", user_id)
    assert provider.visit_location_type == "televisit"
    assert provider.is_televisit is True
    assert provider.appt_address is None
    assert provider.appt_suite is None


def test_appt_wizard_finish_custom_persists_address(client):
    """Custom branch stores the address verbatim (server-side strip happens at render)."""
    test_client, storage, mock_client, user_id = client
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    storage.save_provider(result, user_id)
    resp = test_client.post(
        "/provider/1234567890/appt-wizard",
        data={
            "step": "3",
            "visit_location_type": "custom",
            "appt_address": "1 Shrader St, San Francisco, California 94117, United States",
            "appt_suite": "Suite 6A",
            "appt_phone": "(415) 555-1234",
        },
    )
    assert resp.status_code == 200
    provider = storage.get_provider("1234567890", user_id)
    assert provider.visit_location_type == "custom"
    # Stored verbatim — strip_us applies at render time only.
    assert provider.appt_address.endswith("United States")
    assert provider.appt_suite == "Suite 6A"
    assert provider.appt_phone == "(415) 555-1234"


def test_appt_wizard_finish_custom_requires_address(client):
    """Submitting custom branch with empty address bounces to step 2."""
    test_client, storage, mock_client, user_id = client
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    storage.save_provider(result, user_id)
    resp = test_client.post(
        "/provider/1234567890/appt-wizard",
        data={"step": "3", "visit_location_type": "custom", "appt_address": ""},
    )
    assert resp.status_code == 200
    assert "Enter an address" in resp.text
    provider = storage.get_provider("1234567890", user_id)
    assert provider.visit_location_type is None


def test_appt_wizard_back_navigation(client):
    """'Back' from step 2 re-renders step 1 without persisting anything."""
    test_client, storage, mock_client, user_id = client
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    storage.save_provider(result, user_id)
    resp = test_client.post(
        "/provider/1234567890/appt-wizard",
        data={"step": "back-to-1", "visit_location_type": "custom"},
    )
    assert resp.status_code == 200
    assert 'name="visit_location_type"' in resp.text
    provider = storage.get_provider("1234567890", user_id)
    assert provider.visit_location_type is None


def test_save_provider_opens_wizard_on_first_save(client):
    """A first save bundles a #modal-root OOB swap so the wizard pops open."""
    test_client, storage, mock_client, user_id = client
    mock_client.async_lookup.return_value = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    resp = test_client.post("/provider/1234567890/save")
    assert resp.status_code == 200
    body = resp.text
    assert 'id="modal-root"' in body
    assert "appt-wizard-modal" in body


def test_save_provider_skips_wizard_when_already_configured(client):
    """Re-saving a row that already has visit_location_type doesn't re-open the wizard."""
    test_client, storage, mock_client, user_id = client
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    storage.save_provider(result, user_id)
    storage.set_visit_details(
        "1234567890",
        user_id,
        visit_location_type="practice",
    )
    mock_client.async_lookup.return_value = result
    resp = test_client.post("/provider/1234567890/save")
    assert resp.status_code == 200
    # The "already saved" branch returns just the button — no modal swap.
    assert "modal-root" not in resp.text


def test_saved_page_renders_search_input(client):
    """Saved page includes the client-side search input when providers exist."""
    test_client, storage, mock_client, user_id = client
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    storage.save_provider(result, user_id)
    resp = test_client.get("/rolodex")
    assert resp.status_code == 200
    assert 'id="saved-search"' in resp.text
    assert "Smith" in resp.text


def test_saved_page_empty_has_no_search(client):
    """Saved page with no providers does not show the search input."""
    test_client, storage, mock_client, user_id = client
    resp = test_client.get("/rolodex")
    assert resp.status_code == 200
    assert 'id="saved-search"' not in resp.text
    assert "No providers saved" in resp.text


def test_saved_redirects_to_rolodex(client):
    """Legacy /saved URL 301-redirects to /rolodex."""
    test_client, _, _, _ = client
    resp = test_client.get("/saved", follow_redirects=False)
    assert resp.status_code == 301
    assert resp.headers["location"] == "/rolodex"


def test_saved_export_paths_redirect(client):
    test_client, _, _, _ = client
    for old, new in [
        ("/saved/export", "/rolodex/export"),
        ("/saved/export/csv", "/rolodex/export/csv"),
        ("/saved/export/json", "/rolodex/export/json"),
    ]:
        resp = test_client.get(old, follow_redirects=False)
        assert resp.status_code == 301, f"{old} should redirect"
        assert resp.headers["location"] == new


def test_saved_redirect_preserves_query_string(client):
    """UTM tags / filter params bookmarked against /saved must survive the rename."""
    test_client, _, _, _ = client
    resp = test_client.get("/saved?utm_source=email&x=1", follow_redirects=False)
    assert resp.status_code == 301
    assert resp.headers["location"] == "/rolodex?utm_source=email&x=1"
