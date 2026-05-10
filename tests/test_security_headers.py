"""Phase 15 baseline security headers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from docstats.auth import get_current_user
from docstats.storage import Storage, get_storage
from docstats.web import app, get_client


@pytest.fixture
def client(tmp_path: Path):
    storage = Storage(db_path=tmp_path / "test.db")
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_client] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: None
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_baseline_security_headers_present(client: TestClient):
    resp = client.get("/")
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp.headers.get("X-Frame-Options") == "DENY"
    assert resp.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
    assert resp.headers.get("X-Robots-Tag") == "noindex, nofollow"


def test_csp_header_present_and_locks_dangerous_directives(client: TestClient):
    resp = client.get("/")
    csp = resp.headers.get("Content-Security-Policy", "")
    # The directives that prevent clickjacking, <base> hijacking, and
    # legacy plugin injection MUST be locked down.
    assert "frame-ancestors 'none'" in csp
    assert "base-uri 'self'" in csp
    assert "form-action 'self'" in csp
    assert "object-src 'none'" in csp
    # default-src must be self.
    assert "default-src 'self'" in csp


def test_hsts_only_on_https(client: TestClient):
    # TestClient defaults to http — HSTS must NOT be emitted.
    resp = client.get("/")
    assert "Strict-Transport-Security" not in resp.headers


def test_hsts_emitted_when_forwarded_proto_is_https(client: TestClient):
    # Railway / proxy terminates TLS and forwards X-Forwarded-Proto.
    resp = client.get("/", headers={"X-Forwarded-Proto": "https"})
    assert resp.headers.get("Strict-Transport-Security") == (
        "max-age=31536000; includeSubDomains; preload"
    )
