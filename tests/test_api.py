"""Tests for API helper routes (ZIP lookup, taxonomy list)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from docstats.auth import get_current_user
from docstats.storage import Storage, get_storage
from docstats.web import app, get_client


@pytest.fixture
def api_client(tmp_path: Path):
    storage = Storage(db_path=tmp_path / "test.db")
    mock_nppes = MagicMock()
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_client] = lambda: mock_nppes
    app.dependency_overrides[get_current_user] = lambda: None
    yield TestClient(app), storage
    app.dependency_overrides.clear()


def test_zip_lookup_found(api_client):
    tc, storage = api_client
    # ZIP data is lazy-loaded from src/docstats/data/zipcodes.json on first lookup
    assert storage.lookup_zip("94110") is not None, "ZIP data file missing — cannot test"
    resp = tc.get("/api/zip/94110")
    assert resp.status_code == 200
    data = resp.json()
    assert data["city"] is not None
    assert data["state"] is not None


def test_zip_lookup_not_found(api_client):
    tc, _ = api_client
    resp = tc.get("/api/zip/00000")
    assert resp.status_code == 200
    data = resp.json()
    assert data["city"] is None
    assert data["state"] is None


def test_taxonomy_list(api_client):
    tc, _ = api_client
    resp = tc.get("/api/taxonomies")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) > 100
    assert "cache-control" in resp.headers
