"""Tests for onboarding routes."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from docstats.auth import get_current_user, hash_password
from docstats.routes.onboarding import _onboarding_step
from docstats.storage import Storage, get_storage
from docstats.web import app, get_client


@pytest.fixture
def onboarding_client(tmp_path: Path):
    """TestClient with a fresh user who hasn't completed onboarding."""
    storage = Storage(db_path=tmp_path / "test.db")
    user_id = storage.create_user("new@test.com", hash_password("password123"))
    user = storage.get_user_by_id(user_id)
    mock_nppes = MagicMock()
    mock_nppes.async_search = AsyncMock()
    mock_nppes.async_lookup = AsyncMock()
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_client] = lambda: mock_nppes
    app.dependency_overrides[get_current_user] = lambda: user
    yield TestClient(app), storage, user_id
    app.dependency_overrides.clear()


class TestOnboardingStep:
    def test_step1_no_name(self):
        assert _onboarding_step({"first_name": None, "last_name": None}) == 1

    def test_step2_no_dob(self):
        assert _onboarding_step({"first_name": "A", "last_name": "B", "date_of_birth": None}) == 2

    def test_step3_no_pcp(self):
        assert (
            _onboarding_step(
                {
                    "first_name": "A",
                    "last_name": "B",
                    "date_of_birth": "2000-01-01",
                    "pcp_npi": None,
                }
            )
            == 3
        )

    def test_step3_pcp_skipped(self):
        assert (
            _onboarding_step(
                {
                    "first_name": "A",
                    "last_name": "B",
                    "date_of_birth": "2000-01-01",
                    "pcp_npi": None,
                },
                pcp_skipped=True,
            )
            == 4
        )

    def test_step4_all_present(self):
        assert (
            _onboarding_step(
                {
                    "first_name": "A",
                    "last_name": "B",
                    "date_of_birth": "2000-01-01",
                    "pcp_npi": "1234567890",
                }
            )
            == 4
        )


def test_onboarding_page_renders(onboarding_client):
    tc, _, _ = onboarding_client
    resp = tc.get("/onboarding")
    assert resp.status_code == 200


def test_onboarding_redirects_when_complete(tmp_path: Path):
    """Users with terms_accepted_at skip onboarding."""
    storage = Storage(db_path=tmp_path / "test.db")
    user_id = storage.create_user("done@test.com", hash_password("pw"))
    storage.record_terms_acceptance(
        user_id, terms_version="1.0", ip_address="127.0.0.1", user_agent="test"
    )
    user = storage.get_user_by_id(user_id)
    mock_nppes = MagicMock()
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_client] = lambda: mock_nppes
    app.dependency_overrides[get_current_user] = lambda: user
    tc = TestClient(app)
    resp = tc.get("/onboarding", follow_redirects=False)
    app.dependency_overrides.clear()
    assert resp.status_code == 303


def test_save_name(onboarding_client):
    tc, storage, user_id = onboarding_client
    resp = tc.post(
        "/onboarding/save-name",
        data={
            "first_name": "Jane",
            "last_name": "Doe",
            "middle_name": "",
        },
    )
    assert resp.status_code == 200
    assert resp.headers.get("hx-trigger") == "stepComplete"
    user = storage.get_user_by_id(user_id)
    assert user["first_name"] == "Jane"
    assert user["last_name"] == "Doe"


def test_save_dob(onboarding_client):
    tc, storage, user_id = onboarding_client
    resp = tc.post("/onboarding/save-dob", data={"date_of_birth": "1990-06-15"})
    assert resp.status_code == 200
    assert resp.headers.get("hx-trigger") == "stepComplete"
    user = storage.get_user_by_id(user_id)
    assert user["date_of_birth"] == "1990-06-15"


def test_save_dob_invalid(onboarding_client):
    tc, _, _ = onboarding_client
    resp = tc.post("/onboarding/save-dob", data={"date_of_birth": "not-a-date"})
    assert resp.status_code == 200
    assert "Invalid" in resp.text


def test_save_dob_future(onboarding_client):
    tc, _, _ = onboarding_client
    resp = tc.post("/onboarding/save-dob", data={"date_of_birth": "2099-01-01"})
    assert resp.status_code == 200
    assert "future" in resp.text.lower()


def test_skip_pcp(onboarding_client):
    tc, _, _ = onboarding_client
    resp = tc.get("/onboarding/skip-pcp")
    assert resp.status_code == 200
    assert resp.headers.get("hx-trigger") == "stepComplete"


def test_accept_terms(onboarding_client):
    tc, storage, user_id = onboarding_client
    resp = tc.post("/onboarding/accept-terms", data={"terms_version": "1.0"})
    assert resp.status_code == 200
    assert resp.headers.get("hx-redirect") == "/"
    user = storage.get_user_by_id(user_id)
    assert user["terms_accepted_at"] is not None


def test_skip_onboarding(onboarding_client):
    tc, _, _ = onboarding_client
    resp = tc.get("/onboarding/skip", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
