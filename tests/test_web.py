"""Tests for web route behavior."""

from __future__ import annotations

from unittest.mock import MagicMock
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from docstats.web import app
from docstats.models import NPIResponse, NPIResult
from tests.conftest import SAMPLE_NPI1_RESULT


@pytest.fixture
def client(tmp_path: Path):
    """TestClient with storage and client dependencies overridden."""
    from docstats.storage import Storage
    from docstats.web import get_storage, get_client

    storage = Storage(db_path=tmp_path / "test.db")
    mock_client = MagicMock()
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_client] = lambda: mock_client
    yield TestClient(app), storage, mock_client
    app.dependency_overrides.clear()


def _make_response(results=None):
    results = results or [NPIResult.model_validate(SAMPLE_NPI1_RESULT)]
    return NPIResponse(result_count=len(results), results=results)


def test_search_with_query_param(client):
    test_client, storage, mock_client = client
    mock_client.search.return_value = _make_response()
    resp = test_client.get("/search", params={"query": "dr sarah chen"})
    assert resp.status_code == 200
    assert mock_client.search.called


def test_search_tries_interpretations_until_results(client):
    """If first interpretation returns empty, tries the next one."""
    test_client, storage, mock_client = client
    empty = NPIResponse(result_count=0, results=[])
    full = _make_response()
    mock_client.search.side_effect = [empty, full]
    resp = test_client.get("/search", params={"query": "dr kim do orthopedics"})
    assert resp.status_code == 200
    assert mock_client.search.call_count == 2


def test_search_returns_interp_desc(client):
    """Response HTML includes 'Searched as:' text."""
    test_client, storage, mock_client = client
    mock_client.search.return_value = _make_response()
    resp = test_client.get("/search", params={"query": "dr sarah chen cardiology"})
    assert "Searched as:" in resp.text


def test_appt_address_post(client):
    """POST /provider/{npi}/appt-address saves address."""
    test_client, storage, mock_client = client
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    storage.save_provider(result)
    resp = test_client.post(
        "/provider/1234567890/appt-address",
        data={"address": "1 Shrader St, San Francisco, CA 94117"},
    )
    assert resp.status_code == 200
    provider = storage.get_provider("1234567890")
    assert provider.appt_address == "1 Shrader St, San Francisco, CA 94117"


def test_appt_address_delete(client):
    """DELETE /provider/{npi}/appt-address clears address."""
    test_client, storage, mock_client = client
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    storage.save_provider(result)
    storage.set_appt_address("1234567890", "1 Shrader St, San Francisco, CA 94117")
    resp = test_client.delete("/provider/1234567890/appt-address")
    assert resp.status_code == 200
    provider = storage.get_provider("1234567890")
    assert provider.appt_address is None
