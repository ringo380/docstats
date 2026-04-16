"""Integration tests for input validation boundaries (issue #51)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from docstats.auth import get_current_user, hash_password
from docstats.storage import Storage, get_storage
from docstats.web import app, get_client


@pytest.fixture
def anon_client(tmp_path: Path):
    """Anonymous (not logged-in) TestClient."""
    storage = Storage(db_path=tmp_path / "test.db")
    mock_client = MagicMock()
    mock_client.async_search = AsyncMock()
    mock_client.async_lookup = AsyncMock(return_value=None)
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_client] = lambda: mock_client
    app.dependency_overrides[get_current_user] = lambda: None
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def auth_client(tmp_path: Path):
    """Logged-in TestClient with a real storage + mocked NPPES."""
    storage = Storage(db_path=tmp_path / "test.db")
    user_id = storage.create_user("test@example.com", hash_password("password123"))
    user = storage.get_user_by_id(user_id)
    mock_client = MagicMock()
    mock_client.async_search = AsyncMock()
    mock_client.async_lookup = AsyncMock(return_value=None)
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_client] = lambda: mock_client
    app.dependency_overrides[get_current_user] = lambda: user
    yield TestClient(app), storage, user_id
    app.dependency_overrides.clear()


# --- NPI validation at route boundary -------------------------------------

class TestNpiPathValidation:
    @pytest.mark.parametrize("bad_npi", [
        "abc123",
        "123",
        "12345678901",       # 11 digits
        "1234567890 ",       # trailing space
        "12345abcde",
    ])
    def test_rejects_malformed_npi_detail(self, anon_client, bad_npi):
        resp = anon_client.get(f"/provider/{bad_npi}", follow_redirects=False)
        assert resp.status_code == 422, (
            f"Expected 422 for NPI '{bad_npi}', got {resp.status_code}"
        )

    def test_accepts_well_formed_npi(self, anon_client):
        # Route will return a 404-ish HTML (mock lookup returns None), but NOT 422.
        resp = anon_client.get("/provider/1234567890", follow_redirects=False)
        assert resp.status_code != 422

    def test_rejects_malformed_npi_save(self, auth_client):
        client, _, _ = auth_client
        resp = client.post("/provider/abc/save")
        assert resp.status_code == 422

    def test_rejects_malformed_npi_enrichment(self, anon_client):
        resp = anon_client.get("/provider/abc/enrichment")
        assert resp.status_code == 422

    def test_rejects_header_injection_attempt(self, anon_client):
        # CRLF in path would be URL-encoded by the client but regex still fails.
        resp = anon_client.get("/provider/12345%0d%0aX/export/text")
        assert resp.status_code in (422, 404)


# --- Signup / login field caps -------------------------------------------

class TestSignupValidation:
    def test_rejects_invalid_email_format(self, anon_client):
        resp = anon_client.post("/auth/signup", data={
            "email": "notanemail",
            "password": "password123",
            "confirm_password": "password123",
        })
        assert resp.status_code == 200
        assert b"valid email" in resp.content.lower() or b"invalid" in resp.content.lower()

    def test_rejects_oversize_password(self, anon_client):
        long_pw = "a" * 100  # > 72-byte bcrypt limit
        resp = anon_client.post("/auth/signup", data={
            "email": "new@example.com",
            "password": long_pw,
            "confirm_password": long_pw,
        })
        # FastAPI rejects at the Form boundary with 422
        assert resp.status_code == 422

    def test_rejects_oversize_email(self, anon_client):
        long_email = "a" * 300 + "@example.com"
        resp = anon_client.post("/auth/signup", data={
            "email": long_email,
            "password": "password123",
            "confirm_password": "password123",
        })
        assert resp.status_code == 422

    def test_accepts_valid_signup(self, anon_client):
        resp = anon_client.post(
            "/auth/signup",
            data={
                "email": "new@example.com",
                "password": "password123",
                "confirm_password": "password123",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303  # redirect to onboarding


# --- Search param caps ---------------------------------------------------

class TestSearchQueryCaps:
    def test_rejects_oversize_query(self, anon_client):
        resp = anon_client.get("/search", params={"query": "a" * 1000})
        assert resp.status_code == 422

    def test_rejects_bad_limit(self, anon_client):
        resp = anon_client.get("/search", params={"query": "smith", "limit": 99999})
        assert resp.status_code == 422


# --- Session cookie posture ---------------------------------------------

def test_session_cookie_is_httponly_and_samesite_lax(anon_client):
    # Starlette only emits Set-Cookie when the session is mutated. The
    # anonymous-search counter writes to `request.session`, so hit that.
    from docstats.models import NPIResponse

    # Find the mocked client through the dependency override and set a response.
    from docstats.web import get_client
    app.dependency_overrides[get_client]().async_search.return_value = NPIResponse(
        result_count=0, results=[]
    )

    resp = anon_client.get("/search", params={"name": "smith"})
    cookies = resp.headers.get_list("set-cookie")
    session_cookie = next((c for c in cookies if c.lower().startswith("session=")), "")
    assert session_cookie, "Expected session Set-Cookie header after mutating session"
    lower = session_cookie.lower()
    assert "httponly" in lower
    assert "samesite=lax" in lower
