"""Tests for authentication routes."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from docstats.auth import get_current_user, hash_password
from docstats.storage import Storage, get_storage
from docstats.web import app, get_client


@pytest.fixture
def anon_client(tmp_path: Path):
    """TestClient with no authenticated user."""
    storage = Storage(db_path=tmp_path / "test.db")
    mock_nppes = MagicMock()
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_client] = lambda: mock_nppes
    app.dependency_overrides[get_current_user] = lambda: None
    yield TestClient(app), storage
    app.dependency_overrides.clear()


def test_login_page_renders(anon_client):
    tc, _ = anon_client
    resp = tc.get("/auth/login")
    assert resp.status_code == 200
    assert "Log In" in resp.text or "login" in resp.text.lower()


def test_login_empty_fields(anon_client):
    tc, _ = anon_client
    resp = tc.post("/auth/login", data={"email": "", "password": ""})
    assert resp.status_code == 200
    assert "required" in resp.text.lower()


def test_login_invalid_credentials(anon_client):
    tc, storage = anon_client
    storage.create_user("user@test.com", hash_password("correct123"))
    resp = tc.post("/auth/login", data={"email": "user@test.com", "password": "wrong"})
    assert resp.status_code == 200
    assert "invalid" in resp.text.lower() or "Invalid" in resp.text


def test_login_success_redirects(anon_client):
    tc, storage = anon_client
    storage.create_user("user@test.com", hash_password("correct123"))
    resp = tc.post(
        "/auth/login",
        data={"email": "user@test.com", "password": "correct123"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"


def test_signup_page_renders(anon_client):
    tc, _ = anon_client
    resp = tc.get("/auth/signup")
    assert resp.status_code == 200


def test_signup_password_too_short(anon_client):
    tc, _ = anon_client
    resp = tc.post("/auth/signup", data={
        "email": "new@test.com", "password": "short", "confirm_password": "short",
    })
    assert resp.status_code == 200
    assert "8 characters" in resp.text


def test_signup_password_mismatch(anon_client):
    tc, _ = anon_client
    resp = tc.post("/auth/signup", data={
        "email": "new@test.com", "password": "longpassword", "confirm_password": "different",
    })
    assert resp.status_code == 200
    assert "do not match" in resp.text.lower() or "mismatch" in resp.text.lower()


def test_signup_duplicate_email(anon_client):
    tc, storage = anon_client
    storage.create_user("taken@test.com", hash_password("password123"))
    resp = tc.post("/auth/signup", data={
        "email": "taken@test.com", "password": "password123", "confirm_password": "password123",
    })
    assert resp.status_code == 200
    assert "already exists" in resp.text.lower()


def test_signup_success_redirects_to_onboarding(anon_client):
    tc, _ = anon_client
    resp = tc.post(
        "/auth/signup",
        data={"email": "new@test.com", "password": "password123", "confirm_password": "password123"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/onboarding"


def test_logout_clears_session(anon_client):
    tc, _ = anon_client
    resp = tc.get("/auth/logout", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"


def test_login_page_redirects_when_authenticated(tmp_path: Path):
    """Authenticated users visiting /auth/login get redirected to /."""
    storage = Storage(db_path=tmp_path / "test.db")
    user_id = storage.create_user("user@test.com", hash_password("pw"))
    fake_user = {"id": user_id, "email": "user@test.com"}
    mock_nppes = MagicMock()
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_client] = lambda: mock_nppes
    app.dependency_overrides[get_current_user] = lambda: fake_user
    tc = TestClient(app)
    resp = tc.get("/auth/login", follow_redirects=False)
    app.dependency_overrides.clear()
    assert resp.status_code == 303
