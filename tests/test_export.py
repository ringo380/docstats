"""Tests for export routes (CSV, JSON, single-provider text)."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from docstats.auth import get_current_user
from docstats.models import NPIResult
from docstats.storage import Storage, get_storage
from docstats.web import app, get_client
from tests.conftest import SAMPLE_NPI1_RESULT


@pytest.fixture
def export_client(tmp_path: Path):
    """TestClient with authenticated user and mock NPPES client."""
    storage = Storage(db_path=tmp_path / "test.db")
    user_id = storage.create_user("export@test.com", "hashed_pw")
    fake_user = {
        "id": user_id, "email": "export@test.com",
        "display_name": None, "github_id": None, "github_login": None,
        "password_hash": "hashed_pw", "created_at": "2026-01-01", "last_login_at": None,
    }
    mock_nppes = MagicMock()
    mock_nppes.async_search = AsyncMock()
    mock_nppes.async_lookup = AsyncMock()
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_client] = lambda: mock_nppes
    app.dependency_overrides[get_current_user] = lambda: fake_user
    yield TestClient(app), storage, mock_nppes, user_id
    app.dependency_overrides.clear()


def test_csv_export_empty(export_client):
    """CSV export with no saved providers returns headers only."""
    tc, _, _, _ = export_client
    resp = tc.get("/saved/export/csv")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    reader = csv.reader(io.StringIO(resp.text))
    rows = list(reader)
    assert len(rows) == 1  # header only
    assert "NPI" in rows[0]


def test_csv_export_with_provider(export_client):
    tc, storage, _, user_id = export_client
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    storage.save_provider(result, user_id)
    resp = tc.get("/saved/export/csv")
    assert resp.status_code == 200
    reader = csv.reader(io.StringIO(resp.text))
    rows = list(reader)
    assert len(rows) == 2  # header + 1 row
    assert "1234567890" in rows[1]


def test_json_export_empty(export_client):
    tc, _, _, _ = export_client
    resp = tc.get("/saved/export/json")
    assert resp.status_code == 200
    data = json.loads(resp.text)
    assert data == []


def test_json_export_with_provider(export_client):
    tc, storage, _, user_id = export_client
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    storage.save_provider(result, user_id)
    resp = tc.get("/saved/export/json")
    assert resp.status_code == 200
    data = json.loads(resp.text)
    assert len(data) == 1
    assert data[0]["NPI"] == "1234567890"


def test_single_provider_text_export(export_client):
    tc, storage, mock_nppes, user_id = export_client
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    storage.save_provider(result, user_id)
    resp = tc.get("/provider/1234567890/export/text")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert "1234567890" in resp.text
    assert "content-disposition" in resp.headers


def test_single_provider_text_export_unsaved(export_client):
    """Unsaved provider is looked up via NPPES client."""
    tc, _, mock_nppes, _ = export_client
    mock_nppes.async_lookup.return_value = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    resp = tc.get("/provider/1234567890/export/text")
    assert resp.status_code == 200
    assert mock_nppes.async_lookup.called


def test_export_all_page_renders(export_client):
    tc, _, _, _ = export_client
    resp = tc.get("/saved/export")
    assert resp.status_code == 200
