"""Tests for the /profile signature-fields editor (POST /profile/signature
+ POST/DELETE /profile/signature/image).
"""

from __future__ import annotations

import io
import struct
import zlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from docstats.auth import get_current_user
from docstats.storage import Storage, get_storage
from docstats.storage_files import get_file_backend
from docstats.storage_files.factory import reset_memory_singleton_for_tests
from docstats.storage_files.memory_store import InMemoryFileBackend
from docstats.web import app


def _make_user_row(user_id: int, email: str) -> dict:
    return {
        "id": user_id,
        "email": email,
        "display_name": None,
        "first_name": "Ryan",
        "last_name": "Robson",
        "github_id": None,
        "github_login": None,
        "password_hash": "hashed",
        "created_at": "2026-01-01",
        "last_login_at": None,
        "terms_accepted_at": "2026-01-01",
        "active_org_id": None,
        "is_org_admin": False,
    }


def _seed_user(storage: Storage, user_id: int, email: str) -> None:
    """Insert a minimal users row into SQLite so storage.get_user_by_id works."""
    storage._conn.execute(
        "INSERT INTO users (id, email, password_hash, first_name, last_name) VALUES (?, ?, ?, ?, ?)",
        (user_id, email, "hashed", "Ryan", "Robson"),
    )
    storage._conn.commit()


def _png_bytes() -> bytes:
    """Return a tiny valid PNG (1x1 transparent) so MIME sniff returns image/png."""
    sig = b"\x89PNG\r\n\x1a\n"

    def chunk(name: bytes, payload: bytes) -> bytes:
        crc = zlib.crc32(name + payload)
        return struct.pack(">I", len(payload)) + name + payload + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    raw = b"\x00" + b"\x00\x00\x00\x00"  # filter byte + 4 zero bytes (RGBA)
    idat = zlib.compress(raw, 9)
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    return Storage(db_path=tmp_path / "profile.db")


@pytest.fixture
def memory_backend(monkeypatch: pytest.MonkeyPatch) -> InMemoryFileBackend:
    monkeypatch.setenv("ATTACHMENT_STORAGE_BACKEND", "memory")
    reset_memory_singleton_for_tests()
    backend = get_file_backend()
    assert isinstance(backend, InMemoryFileBackend)
    return backend


@pytest.fixture
def client(storage: Storage, memory_backend: InMemoryFileBackend) -> TestClient:
    user_id = 42
    _seed_user(storage, user_id, "ryan@example.com")
    user_row = _make_user_row(user_id, "ryan@example.com")
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_current_user] = lambda: user_row
    app.dependency_overrides[get_file_backend] = lambda: memory_backend
    yield TestClient(app)
    app.dependency_overrides.clear()


# ─── Text-field save ───────────────────────────────────────────────


def test_save_signature_persists_all_fields(client: TestClient, storage: Storage) -> None:
    resp = client.post(
        "/profile/signature",
        data={
            "credentials": "MD, FAAFP",
            "individual_npi": "1234567890",
            "state_license_number": "A-12345",
            "state_license_state": "CA",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/profile?signature_saved=1"

    row = storage.get_user_by_id(42)
    assert row["credentials"] == "MD, FAAFP"
    assert row["individual_npi"] == "1234567890"
    assert row["state_license_number"] == "A-12345"
    assert row["state_license_state"] == "CA"


def test_save_signature_clears_empty_fields(client: TestClient, storage: Storage) -> None:
    # First populate every field.
    storage.update_user_signature(
        42,
        credentials="MD",
        individual_npi="1234567890",
        state_license_number="X1",
        state_license_state="CA",
    )
    # Then submit blanks — replace-all semantics should clear them.
    resp = client.post(
        "/profile/signature",
        data={
            "credentials": "",
            "individual_npi": "",
            "state_license_number": "",
            "state_license_state": "",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    row = storage.get_user_by_id(42)
    assert row["credentials"] is None
    assert row["individual_npi"] is None
    assert row["state_license_number"] is None
    assert row["state_license_state"] is None


def test_save_signature_rejects_bad_npi(client: TestClient) -> None:
    resp = client.post(
        "/profile/signature",
        data={
            "credentials": "",
            "individual_npi": "12345",  # too short
            "state_license_number": "",
            "state_license_state": "",
        },
    )
    assert resp.status_code == 422
    assert "10 digits" in resp.text


def test_save_signature_rejects_unknown_state(client: TestClient) -> None:
    resp = client.post(
        "/profile/signature",
        data={
            "credentials": "",
            "individual_npi": "",
            "state_license_number": "",
            "state_license_state": "ZZ",
        },
    )
    assert resp.status_code == 422
    assert "Unknown state" in resp.text


def test_save_signature_normalizes_state_to_uppercase(client: TestClient, storage: Storage) -> None:
    resp = client.post(
        "/profile/signature",
        data={
            "credentials": "",
            "individual_npi": "",
            "state_license_number": "Z9",
            "state_license_state": "ca",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    row = storage.get_user_by_id(42)
    assert row["state_license_state"] == "CA"


def test_save_signature_records_audit(client: TestClient, storage: Storage) -> None:
    client.post(
        "/profile/signature",
        data={
            "credentials": "MD",
            "individual_npi": "1234567890",
            "state_license_number": "",
            "state_license_state": "",
        },
    )
    events = storage.list_audit_events(actor_user_id=42, limit=10)
    actions = [e.action for e in events]
    assert "user.signature_updated" in actions


# ─── Image upload + delete ─────────────────────────────────────────


def test_upload_signature_image_persists_ref_and_object(
    client: TestClient, storage: Storage, memory_backend: InMemoryFileBackend
) -> None:
    png = _png_bytes()
    resp = client.post(
        "/profile/signature/image",
        files={"file": ("sig.png", io.BytesIO(png), "image/png")},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    row = storage.get_user_by_id(42)
    ref = row["signature_image_ref"]
    assert ref is not None
    assert ref.startswith("user-42/signature/")
    assert ref.endswith(".png")
    # Backend has the bytes (stored as (data, mime_type) tuples).
    assert memory_backend._store[ref][0] == png
    assert memory_backend._store[ref][1] == "image/png"


def test_upload_signature_image_rejects_pdf(client: TestClient) -> None:
    # Minimal PDF header that sniff_mime accepts.
    fake_pdf = b"%PDF-1.4\n%mock\n"
    resp = client.post(
        "/profile/signature/image",
        files={"file": ("sig.pdf", io.BytesIO(fake_pdf), "application/pdf")},
    )
    assert resp.status_code == 415
    assert "PNG or JPEG" in resp.text


def test_upload_signature_image_rejects_too_large(client: TestClient) -> None:
    # 250 KB > the 200 KB cap.
    blob = b"\x89PNG\r\n\x1a\n" + b"\x00" * (250 * 1024)
    resp = client.post(
        "/profile/signature/image",
        files={"file": ("big.png", io.BytesIO(blob), "image/png")},
    )
    assert resp.status_code == 413


def test_upload_replaces_prior_image_and_cleans_old_blob(
    client: TestClient, storage: Storage, memory_backend: InMemoryFileBackend
) -> None:
    png = _png_bytes()
    client.post(
        "/profile/signature/image",
        files={"file": ("first.png", io.BytesIO(png), "image/png")},
    )
    first_ref = storage.get_user_by_id(42)["signature_image_ref"]
    assert first_ref in memory_backend._store

    client.post(
        "/profile/signature/image",
        files={"file": ("second.png", io.BytesIO(png), "image/png")},
    )
    second_ref = storage.get_user_by_id(42)["signature_image_ref"]
    assert second_ref != first_ref
    # Old blob is gone, new blob is present.
    assert first_ref not in memory_backend._store
    assert second_ref in memory_backend._store


def test_clear_signature_image_removes_ref_and_blob(
    client: TestClient, storage: Storage, memory_backend: InMemoryFileBackend
) -> None:
    png = _png_bytes()
    client.post(
        "/profile/signature/image",
        files={"file": ("sig.png", io.BytesIO(png), "image/png")},
    )
    ref = storage.get_user_by_id(42)["signature_image_ref"]
    assert ref in memory_backend._store

    resp = client.delete("/profile/signature/image", follow_redirects=False)
    assert resp.status_code == 303
    assert storage.get_user_by_id(42)["signature_image_ref"] is None
    assert ref not in memory_backend._store


def test_clear_signature_image_when_none_set(client: TestClient, storage: Storage) -> None:
    """Clearing a never-set image is a no-op (audit logged either way)."""
    assert storage.get_user_by_id(42)["signature_image_ref"] is None
    resp = client.delete("/profile/signature/image", follow_redirects=False)
    assert resp.status_code == 303
    events = storage.list_audit_events(actor_user_id=42, limit=10)
    assert "user.signature_image_cleared" in [e.action for e in events]


# ─── /profile renders the editor ─────────────────────────────────────


def test_profile_get_renders_signature_editor(client: TestClient) -> None:
    resp = client.get("/profile")
    assert resp.status_code == 200
    body = resp.text
    assert "Letter signature" in body
    assert 'name="credentials"' in body
    assert 'name="individual_npi"' in body
    assert 'name="state_license_number"' in body
    assert 'name="state_license_state"' in body


def test_profile_get_shows_saved_banner(client: TestClient) -> None:
    resp = client.get("/profile?signature_saved=1")
    assert resp.status_code == 200
    assert "Signature saved." in resp.text


def test_profile_get_shows_image_preview_when_uploaded(
    client: TestClient, storage: Storage
) -> None:
    png = _png_bytes()
    client.post(
        "/profile/signature/image",
        files={"file": ("sig.png", io.BytesIO(png), "image/png")},
    )
    resp = client.get("/profile")
    assert resp.status_code == 200
    body = resp.text
    assert "signature preview" in body
    assert "Remove image" in body
