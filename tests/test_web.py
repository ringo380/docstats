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


def test_appt_address_post(client):
    """POST /provider/{npi}/appt-address saves address."""
    test_client, storage, mock_client, user_id = client
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    storage.save_provider(result, user_id)
    resp = test_client.post(
        "/provider/1234567890/appt-address",
        data={"address": "1 Shrader St, San Francisco, CA 94117"},
    )
    assert resp.status_code == 200
    provider = storage.get_provider("1234567890", user_id)
    assert provider.appt_address == "1 Shrader St, San Francisco, CA 94117"


def test_appt_address_post_unsaved_provider(client):
    """POST for an unsaved provider returns an error message, does not silently drop."""
    test_client, storage, mock_client, user_id = client
    resp = test_client.post(
        "/provider/9999999999/appt-address",
        data={"address": "1 Shrader St, San Francisco, CA 94117"},
    )
    assert resp.status_code == 200
    assert "saved" in resp.text.lower()
    assert storage.get_provider("9999999999", user_id) is None


def test_appt_address_delete(client):
    """DELETE /provider/{npi}/appt-address clears address."""
    test_client, storage, mock_client, user_id = client
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    storage.save_provider(result, user_id)
    storage.set_appt_address("1234567890", "1 Shrader St, San Francisco, CA 94117", user_id)
    resp = test_client.delete("/provider/1234567890/appt-address")
    assert resp.status_code == 200
    provider = storage.get_provider("1234567890", user_id)
    assert provider.appt_address is None


def test_appt_suite_put(client):
    """PUT /provider/{npi}/appt-suite saves suite."""
    test_client, storage, mock_client, user_id = client
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    storage.save_provider(result, user_id)
    storage.set_appt_address("1234567890", "1 Shrader St, San Francisco, CA 94117", user_id)
    resp = test_client.put(
        "/provider/1234567890/appt-suite",
        data={"suite": "Suite 6A"},
    )
    assert resp.status_code == 200
    provider = storage.get_provider("1234567890", user_id)
    assert provider.appt_suite == "Suite 6A"


def test_appt_suite_put_empty_clears(client):
    """PUT with empty suite clears it."""
    test_client, storage, mock_client, user_id = client
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    storage.save_provider(result, user_id)
    storage.set_appt_suite("1234567890", "Suite 6A", user_id)
    resp = test_client.put(
        "/provider/1234567890/appt-suite",
        data={"suite": ""},
    )
    assert resp.status_code == 200
    provider = storage.get_provider("1234567890", user_id)
    assert provider.appt_suite is None


def test_appt_address_delete_clears_suite(client):
    """DELETE /provider/{npi}/appt-address also clears suite."""
    test_client, storage, mock_client, user_id = client
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    storage.save_provider(result, user_id)
    storage.set_appt_address("1234567890", "1 Shrader St, San Francisco, CA 94117", user_id)
    storage.set_appt_suite("1234567890", "Room 201", user_id)
    resp = test_client.delete("/provider/1234567890/appt-address")
    assert resp.status_code == 200
    provider = storage.get_provider("1234567890", user_id)
    assert provider.appt_address is None
    assert provider.appt_suite is None


def test_appt_address_delete_clears_phone_fax(client):
    """DELETE /provider/{npi}/appt-address also clears phone and fax."""
    test_client, storage, mock_client, user_id = client
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    storage.save_provider(result, user_id)
    storage.set_appt_address("1234567890", "1 Shrader St, San Francisco, CA 94117", user_id)
    storage.set_appt_contact("1234567890", "555-1234", "555-5678", user_id)
    resp = test_client.delete("/provider/1234567890/appt-address")
    assert resp.status_code == 200
    provider = storage.get_provider("1234567890", user_id)
    assert provider.appt_phone is None
    assert provider.appt_fax is None


def test_televisit_toggle_on(client):
    """PUT /provider/{npi}/televisit with on sets the flag."""
    test_client, storage, mock_client, user_id = client
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    storage.save_provider(result, user_id)
    resp = test_client.put(
        "/provider/1234567890/televisit",
        data={"is_televisit": "on"},
    )
    assert resp.status_code == 200
    provider = storage.get_provider("1234567890", user_id)
    assert provider.is_televisit is True


def test_televisit_toggle_off(client):
    """PUT /provider/{npi}/televisit with off clears the flag."""
    test_client, storage, mock_client, user_id = client
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    storage.save_provider(result, user_id)
    storage.set_televisit("1234567890", True, user_id)
    resp = test_client.put(
        "/provider/1234567890/televisit",
        data={"is_televisit": "off"},
    )
    assert resp.status_code == 200
    provider = storage.get_provider("1234567890", user_id)
    assert provider.is_televisit is False


def test_televisit_on_clears_address(client):
    """Toggling televisit ON must clear the appointment address."""
    test_client, storage, mock_client, user_id = client
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    storage.save_provider(result, user_id)
    storage.set_appt_address("1234567890", "1 Shrader St, San Francisco, CA 94117", user_id)
    storage.set_appt_contact("1234567890", "555-1234", "555-5678", user_id)
    resp = test_client.put(
        "/provider/1234567890/televisit",
        data={"is_televisit": "on"},
    )
    assert resp.status_code == 200
    provider = storage.get_provider("1234567890", user_id)
    assert provider.is_televisit is True
    assert provider.appt_address is None
    assert provider.appt_phone is None
    assert provider.appt_fax is None


def test_appt_contact_put(client):
    """PUT /provider/{npi}/appt-contact saves phone and fax."""
    test_client, storage, mock_client, user_id = client
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    storage.save_provider(result, user_id)
    resp = test_client.put(
        "/provider/1234567890/appt-contact",
        data={"phone": "(555) 123-4567", "fax": "(555) 987-6543"},
    )
    assert resp.status_code == 200
    provider = storage.get_provider("1234567890", user_id)
    assert provider.appt_phone == "(555) 123-4567"
    assert provider.appt_fax == "(555) 987-6543"


def test_appt_contact_put_empty_clears(client):
    """PUT with empty phone/fax clears them."""
    test_client, storage, mock_client, user_id = client
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    storage.save_provider(result, user_id)
    storage.set_appt_contact("1234567890", "555-1234", "555-5678", user_id)
    resp = test_client.put(
        "/provider/1234567890/appt-contact",
        data={"phone": "", "fax": ""},
    )
    assert resp.status_code == 200
    provider = storage.get_provider("1234567890", user_id)
    assert provider.appt_phone is None
    assert provider.appt_fax is None


def test_appt_address_post_with_phone(client):
    """POST /provider/{npi}/appt-address with phone auto-populates appt_phone."""
    test_client, storage, mock_client, user_id = client
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    storage.save_provider(result, user_id)
    resp = test_client.post(
        "/provider/1234567890/appt-address",
        data={"address": "UTMB Galveston", "phone": "(409) 772-1234"},
    )
    assert resp.status_code == 200
    provider = storage.get_provider("1234567890", user_id)
    assert provider.appt_address == "UTMB Galveston"
    assert provider.appt_phone == "(409) 772-1234"


def test_appt_address_post_with_phone_preserves_fax(client):
    """POI phone auto-fill must not overwrite an existing fax."""
    test_client, storage, mock_client, user_id = client
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    storage.save_provider(result, user_id)
    storage.set_appt_contact("1234567890", None, "555-FAX", user_id)
    resp = test_client.post(
        "/provider/1234567890/appt-address",
        data={"address": "New Clinic", "phone": "555-NEW"},
    )
    assert resp.status_code == 200
    provider = storage.get_provider("1234567890", user_id)
    assert provider.appt_phone == "555-NEW"
    assert provider.appt_fax == "555-FAX"


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
