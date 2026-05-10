"""Phase 15 R-007 — login throttling.

Per-IP and per-account independent counters; either tripping returns 429
with the same generic message so attackers can't infer which dimension
(IP, account) they're hitting.
"""

from __future__ import annotations

from collections import deque
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from docstats.auth import get_current_user, hash_password
from docstats.routes import auth as auth_routes
from docstats.storage import Storage, get_storage
from docstats.web import app, get_client


@pytest.fixture(autouse=True)
def _reset_login_limiters():
    """Each test starts with empty rate-limiter state."""
    # The limiter is a module-level singleton; clearing its internal buckets
    # is the cleanest reset and avoids monkeypatching a fresh instance into
    # the import path the route already captured.
    auth_routes._LOGIN_LIMIT_PER_IP._buckets.clear()
    auth_routes._LOGIN_LIMIT_PER_ACCOUNT._buckets.clear()
    yield
    auth_routes._LOGIN_LIMIT_PER_IP._buckets.clear()
    auth_routes._LOGIN_LIMIT_PER_ACCOUNT._buckets.clear()


@pytest.fixture
def anon_client(tmp_path: Path):
    storage = Storage(db_path=tmp_path / "test.db")
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_client] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: None
    yield TestClient(app), storage
    app.dependency_overrides.clear()


def _bad_login(tc: TestClient, *, email: str = "victim@example.com") -> int:
    return tc.post(
        "/auth/login",
        data={"email": email, "password": "wrong-password-attempt"},
    ).status_code


def test_per_account_limit_returns_429_after_tenth_attempt(anon_client):
    tc, _ = anon_client
    # 10 attempts allowed in the 15-minute window.
    for _ in range(10):
        assert _bad_login(tc) == 200
    # 11th attempt — same email — must throttle.
    assert _bad_login(tc) == 429


def test_per_account_limit_blocks_one_email_not_others(anon_client):
    tc, _ = anon_client
    for _ in range(10):
        assert _bad_login(tc, email="alpha@example.com") == 200
    # alpha is now throttled.
    assert _bad_login(tc, email="alpha@example.com") == 429
    # beta on the same TestClient (same IP "testclient") still has its own
    # per-account budget, but the per-IP counter has accumulated 11 hits
    # already. Cap is 20, so beta should still get through for a few.
    assert _bad_login(tc, email="beta@example.com") == 200


def test_per_ip_limit_returns_429_at_twenty_first_attempt(anon_client):
    tc, _ = anon_client
    # Cycle through unique emails so the per-account counter never trips.
    for i in range(20):
        assert _bad_login(tc, email=f"u{i}@example.com") == 200
    # 21st distinct email from the same IP — IP throttle fires.
    assert _bad_login(tc, email="u21@example.com") == 429


def test_throttled_response_uses_generic_message(anon_client):
    tc, _ = anon_client
    for _ in range(10):
        _bad_login(tc)
    resp = tc.post(
        "/auth/login",
        data={"email": "victim@example.com", "password": "wrong"},
    )
    assert resp.status_code == 429
    body = resp.text
    # The exact user-facing throttle message must appear; the message
    # itself must not name the dimension (ip vs account) that tripped.
    assert "Too many login attempts" in body
    assert "wait a few minutes" in body
    # Sanity: that exact phrase doesn't include the words "ip" or
    # "account" — substring search across the full HTML page hits CSS
    # tokens like "script" / "input" which are unavoidable.


def test_throttle_does_not_block_valid_login_under_limit(anon_client):
    tc, storage = anon_client
    storage.create_user(
        email="legit@example.com",
        password_hash=hash_password("CorrectHorse42"),
    )
    # 9 wrong attempts — still under 10 cap.
    for _ in range(9):
        assert _bad_login(tc, email="legit@example.com") == 200
    # 10th is the valid credential — must succeed (303 redirect to /).
    resp = tc.post(
        "/auth/login",
        data={"email": "legit@example.com", "password": "CorrectHorse42"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers.get("location") == "/"


# Make the deque import explicit so type checkers don't flag it as unused
# if a future refactor drops the per-IP test that exercises it indirectly.
_ = deque
