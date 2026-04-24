"""Phase 10.A — Attachment upload + download + storage-file backend tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from docstats.auth import get_current_user
from docstats.phi import CURRENT_PHI_CONSENT_VERSION
from docstats.scope import Scope
from docstats.storage import Storage, get_storage
from docstats.storage_files import (
    ALLOWED_MIME_TYPES,
    FileNotFoundInBackend,
    InMemoryFileBackend,
    MimeSniffError,
    build_object_path,
    sniff_mime,
)
from docstats.storage_files.factory import (
    get_file_backend,
    reset_memory_singleton_for_tests,
)
from docstats.web import app


# ---------- Sample bytes (just enough to trip the sniffer) ----------

_PDF_BYTES = b"%PDF-1.4\n<fake pdf body>"
_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
_JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 32
_TIFF_BYTES = b"II*\x00" + b"\x00" * 32
_DOCX_BYTES = b"PK\x03\x04" + b"\x00" * 64 + b"word/document.xml" + b"\x00" * 64
_ZIP_NON_DOCX = b"PK\x03\x04" + b"\x00" * 2048  # ZIP magic but no word/ marker
_JUNK = b"\x00\x01\x02\x03"


# ---------- MIME sniffer ----------


def test_sniff_mime_pdf() -> None:
    assert sniff_mime(_PDF_BYTES) == "application/pdf"


def test_sniff_mime_png() -> None:
    assert sniff_mime(_PNG_BYTES) == "image/png"


def test_sniff_mime_jpeg() -> None:
    assert sniff_mime(_JPEG_BYTES) == "image/jpeg"


def test_sniff_mime_tiff() -> None:
    assert sniff_mime(_TIFF_BYTES) == "image/tiff"


def test_sniff_mime_docx() -> None:
    assert (
        sniff_mime(_DOCX_BYTES)
        == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )


def test_sniff_mime_rejects_plain_zip() -> None:
    """ZIP archives without the word/ marker are not allow-listed."""
    with pytest.raises(MimeSniffError):
        sniff_mime(_ZIP_NON_DOCX)


def test_sniff_mime_rejects_junk() -> None:
    with pytest.raises(MimeSniffError):
        sniff_mime(_JUNK)


def test_sniff_mime_rejects_empty() -> None:
    with pytest.raises(MimeSniffError):
        sniff_mime(b"")


def test_all_sniffable_mimes_are_allow_listed() -> None:
    """Regression: sniffer should never return a MIME that the upload
    route is going to reject downstream."""
    detected = {
        sniff_mime(b) for b in (_PDF_BYTES, _PNG_BYTES, _JPEG_BYTES, _TIFF_BYTES, _DOCX_BYTES)
    }
    assert detected <= ALLOWED_MIME_TYPES


# ---------- build_object_path ----------


def test_build_object_path_org_scope() -> None:
    scope = Scope(organization_id=7, membership_role="admin")
    p = build_object_path(scope=scope, referral_id=42, attachment_id=9, mime_type="application/pdf")
    assert p == "org-7/42/9.pdf"


def test_build_object_path_solo_scope() -> None:
    scope = Scope(user_id=3)
    p = build_object_path(scope=scope, referral_id=11, attachment_id=2, mime_type="image/png")
    assert p == "user-3/11/2.png"


def test_build_object_path_rejects_anonymous() -> None:
    from docstats.storage_files.base import StorageFileError

    with pytest.raises(StorageFileError):
        build_object_path(
            scope=Scope(),
            referral_id=1,
            attachment_id=1,
            mime_type="application/pdf",
        )


def test_build_object_path_unknown_mime_falls_to_bin() -> None:
    scope = Scope(user_id=1)
    p = build_object_path(
        scope=scope, referral_id=1, attachment_id=1, mime_type="application/unknown"
    )
    assert p.endswith(".bin")


# ---------- InMemoryFileBackend ----------


def test_in_memory_backend_round_trip() -> None:
    backend = InMemoryFileBackend()

    async def go() -> None:
        ref = await backend.put(path="a/b/c.pdf", data=_PDF_BYTES, mime_type="application/pdf")
        assert ref.storage_ref == "a/b/c.pdf"
        assert ref.size_bytes == len(_PDF_BYTES)
        got = await backend.get_bytes("a/b/c.pdf")
        assert got == _PDF_BYTES
        url = await backend.signed_url("a/b/c.pdf")
        assert url.startswith("inmemory://")

    asyncio.run(go())


def test_in_memory_backend_get_missing_raises() -> None:
    backend = InMemoryFileBackend()

    async def go() -> None:
        with pytest.raises(FileNotFoundInBackend):
            await backend.get_bytes("nope")

    asyncio.run(go())


def test_in_memory_backend_delete_is_idempotent() -> None:
    backend = InMemoryFileBackend()

    async def go() -> None:
        await backend.delete("never-existed")  # must not raise
        await backend.put(path="x", data=b"z", mime_type="application/pdf")
        await backend.delete("x")
        with pytest.raises(FileNotFoundInBackend):
            await backend.get_bytes("x")

    asyncio.run(go())


# ---------- Factory ----------


def test_factory_returns_memory_when_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_memory_singleton_for_tests()
    monkeypatch.setenv("ATTACHMENT_STORAGE_BACKEND", "memory")
    backend = get_file_backend()
    assert isinstance(backend, InMemoryFileBackend)


def test_factory_returns_memory_when_no_supabase_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_memory_singleton_for_tests()
    monkeypatch.delenv("ATTACHMENT_STORAGE_BACKEND", raising=False)
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
    backend = get_file_backend()
    assert isinstance(backend, InMemoryFileBackend)


def test_factory_memory_singleton_is_shared(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_memory_singleton_for_tests()
    monkeypatch.setenv("ATTACHMENT_STORAGE_BACKEND", "memory")
    a = get_file_backend()
    b = get_file_backend()
    assert a is b


# ---------- Storage: get_referral_attachment ----------


def _seed(storage: Storage, user_id: int) -> tuple[Scope, Any, Any, Any]:
    scope = Scope(user_id=user_id)
    patient = storage.create_patient(
        scope,
        first_name="Jane",
        last_name="Doe",
        date_of_birth="1980-01-01",
        created_by_user_id=user_id,
    )
    referral = storage.create_referral(
        scope,
        patient_id=patient.id,
        reason="Consult",
        urgency="routine",
        specialty_desc="Cardiology",
        receiving_organization_name="Heart",
        created_by_user_id=user_id,
    )
    att = storage.add_referral_attachment(
        scope,
        referral.id,
        kind="lab",
        label="Lab",
        checklist_only=True,
    )
    return scope, patient, referral, att


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    return Storage(db_path=tmp_path / "test.db")


def test_get_referral_attachment_found(storage: Storage) -> None:
    user_id = storage.create_user("u@example.com", "hashed")
    scope, _, _, att = _seed(storage, user_id)
    row = storage.get_referral_attachment(scope, att.id)
    assert row is not None
    assert row.id == att.id


def test_get_referral_attachment_missing_returns_none(storage: Storage) -> None:
    user_id = storage.create_user("u@example.com", "hashed")
    scope = Scope(user_id=user_id)
    assert storage.get_referral_attachment(scope, 9999) is None


def test_get_referral_attachment_cross_scope_returns_none(storage: Storage) -> None:
    """User A's attachment must not leak to user B's scope."""
    a = storage.create_user("a@example.com", "hashed")
    b = storage.create_user("b@example.com", "hashed")
    _, _, _, att = _seed(storage, a)
    scope_b = Scope(user_id=b)
    assert storage.get_referral_attachment(scope_b, att.id) is None


# ---------- Route-level tests ----------


def _fake_user(user_id: int, email: str = "u@example.com") -> dict:
    return {
        "id": user_id,
        "email": email,
        "display_name": None,
        "first_name": "U",
        "last_name": "X",
        "github_id": None,
        "github_login": None,
        "password_hash": "h",
        "created_at": "2026-01-01",
        "last_login_at": None,
        "terms_accepted_at": "2026-01-01",
        "phi_consent_at": "2026-01-01",
        "phi_consent_version": CURRENT_PHI_CONSENT_VERSION,
        "phi_consent_ip": None,
        "phi_consent_user_agent": None,
        "active_org_id": None,
    }


@pytest.fixture
def client_env(storage: Storage, monkeypatch: pytest.MonkeyPatch):
    """A TestClient with in-memory backend + uploads enabled + PHI consent."""
    reset_memory_singleton_for_tests()
    monkeypatch.setenv("ATTACHMENT_UPLOAD_ENABLED", "1")
    monkeypatch.setenv("ATTACHMENT_STORAGE_BACKEND", "memory")
    user_id = storage.create_user("u@example.com", "hashed")
    storage.record_phi_consent(
        user_id=user_id,
        phi_consent_version=CURRENT_PHI_CONSENT_VERSION,
        ip_address="",
        user_agent="",
    )
    scope, patient, referral, _ = _seed(storage, user_id)
    backend = InMemoryFileBackend()
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_current_user] = lambda: _fake_user(user_id)
    app.dependency_overrides[get_file_backend] = lambda: backend
    try:
        yield TestClient(app), storage, user_id, referral, backend
    finally:
        app.dependency_overrides.clear()


def test_upload_disabled_returns_404(storage: Storage, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ATTACHMENT_UPLOAD_ENABLED", raising=False)
    user_id = storage.create_user("u@example.com", "hashed")
    storage.record_phi_consent(
        user_id=user_id,
        phi_consent_version=CURRENT_PHI_CONSENT_VERSION,
        ip_address="",
        user_agent="",
    )
    _, _, referral, _ = _seed(storage, user_id)
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_current_user] = lambda: _fake_user(user_id)
    try:
        tc = TestClient(app)
        resp = tc.post(
            f"/referrals/{referral.id}/attachments",
            data={"kind": "lab", "label": "x"},
            files={"file": ("x.pdf", _PDF_BYTES, "application/pdf")},
            follow_redirects=False,
        )
        assert resp.status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_upload_happy_path(client_env) -> None:
    tc, storage, user_id, referral, backend = client_env
    resp = tc.post(
        f"/referrals/{referral.id}/attachments",
        data={"kind": "lab", "label": "Chest X-ray report"},
        files={"file": ("x.pdf", _PDF_BYTES, "application/pdf")},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/referrals/{referral.id}"

    # Storage row persisted with storage_ref + checklist_only=False
    scope = Scope(user_id=user_id)
    rows = storage.list_referral_attachments(scope, referral.id)
    # _seed already added one checklist-only row; the newly-uploaded is the newest.
    uploaded = [r for r in rows if r.storage_ref]
    assert len(uploaded) == 1
    assert uploaded[0].storage_ref.startswith(f"user-{user_id}/{referral.id}/")
    assert uploaded[0].storage_ref.endswith(".pdf")
    assert uploaded[0].checklist_only is False
    assert uploaded[0].label == "Chest X-ray report"

    # Backend has the bytes at the expected path.
    assert backend._has(uploaded[0].storage_ref)

    # Audit row.
    events = storage.list_audit_events(scope_user_id=user_id, action="attachment.create")
    assert len(events) == 1


def test_upload_rejects_bad_mime(client_env) -> None:
    tc, _, _, referral, _ = client_env
    resp = tc.post(
        f"/referrals/{referral.id}/attachments",
        data={"kind": "lab", "label": "bad"},
        files={"file": ("junk.bin", _JUNK, "application/octet-stream")},
        follow_redirects=False,
    )
    assert resp.status_code == 415


def test_upload_rejects_empty_file(client_env) -> None:
    tc, _, _, referral, _ = client_env
    resp = tc.post(
        f"/referrals/{referral.id}/attachments",
        data={"kind": "lab", "label": "empty"},
        files={"file": ("empty.pdf", b"", "application/pdf")},
        follow_redirects=False,
    )
    assert resp.status_code == 422


def test_upload_rejects_bad_kind(client_env) -> None:
    tc, _, _, referral, _ = client_env
    resp = tc.post(
        f"/referrals/{referral.id}/attachments",
        data={"kind": "pigeon", "label": "x"},
        files={"file": ("x.pdf", _PDF_BYTES, "application/pdf")},
        follow_redirects=False,
    )
    assert resp.status_code == 422


def test_upload_rejects_blank_label(client_env) -> None:
    tc, _, _, referral, _ = client_env
    resp = tc.post(
        f"/referrals/{referral.id}/attachments",
        data={"kind": "lab", "label": "   "},
        files={"file": ("x.pdf", _PDF_BYTES, "application/pdf")},
        follow_redirects=False,
    )
    assert resp.status_code == 422


def test_upload_rejects_oversized_via_content_length(client_env, monkeypatch) -> None:
    tc, _, _, referral, _ = client_env
    # Fake the content-length header to simulate an oversized body without
    # actually transmitting 50 MB.
    resp = tc.post(
        f"/referrals/{referral.id}/attachments",
        data={"kind": "lab", "label": "huge"},
        files={"file": ("x.pdf", _PDF_BYTES, "application/pdf")},
        headers={"content-length": str(100 * 1024 * 1024)},
        follow_redirects=False,
    )
    assert resp.status_code == 413


def test_upload_404_on_missing_referral(client_env) -> None:
    tc, _, _, _, _ = client_env
    resp = tc.post(
        "/referrals/999999/attachments",
        data={"kind": "lab", "label": "x"},
        files={"file": ("x.pdf", _PDF_BYTES, "application/pdf")},
        follow_redirects=False,
    )
    assert resp.status_code == 404


def test_download_redirects_to_signed_url(client_env) -> None:
    tc, storage, user_id, referral, _ = client_env
    # First upload to get an attachment row with bytes.
    tc.post(
        f"/referrals/{referral.id}/attachments",
        data={"kind": "lab", "label": "Report"},
        files={"file": ("x.pdf", _PDF_BYTES, "application/pdf")},
        follow_redirects=False,
    )
    scope = Scope(user_id=user_id)
    [row] = [r for r in storage.list_referral_attachments(scope, referral.id) if r.storage_ref]

    resp = tc.get(f"/attachments/{row.id}", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"].startswith("inmemory://")

    # Audit recorded.
    events = storage.list_audit_events(scope_user_id=user_id, action="attachment.view")
    assert len(events) == 1


def test_download_404_cross_scope(client_env, storage: Storage) -> None:
    tc, _, user_id, referral, _ = client_env
    tc.post(
        f"/referrals/{referral.id}/attachments",
        data={"kind": "lab", "label": "Report"},
        files={"file": ("x.pdf", _PDF_BYTES, "application/pdf")},
        follow_redirects=False,
    )
    scope_a = Scope(user_id=user_id)
    [row] = [r for r in storage.list_referral_attachments(scope_a, referral.id) if r.storage_ref]

    # Swap to a different user — should 404.
    other = storage.create_user("other@example.com", "hashed")
    storage.record_phi_consent(
        user_id=other,
        phi_consent_version=CURRENT_PHI_CONSENT_VERSION,
        ip_address="",
        user_agent="",
    )
    app.dependency_overrides[get_current_user] = lambda: _fake_user(other, "other@example.com")
    resp = tc.get(f"/attachments/{row.id}", follow_redirects=False)
    assert resp.status_code == 404


def test_download_disabled_returns_404(client_env, monkeypatch: pytest.MonkeyPatch) -> None:
    tc, storage, user_id, referral, _ = client_env
    tc.post(
        f"/referrals/{referral.id}/attachments",
        data={"kind": "lab", "label": "Report"},
        files={"file": ("x.pdf", _PDF_BYTES, "application/pdf")},
        follow_redirects=False,
    )
    scope = Scope(user_id=user_id)
    [row] = [r for r in storage.list_referral_attachments(scope, referral.id) if r.storage_ref]
    monkeypatch.delenv("ATTACHMENT_UPLOAD_ENABLED", raising=False)
    resp = tc.get(f"/attachments/{row.id}", follow_redirects=False)
    assert resp.status_code == 404


def test_delete_happy_path(client_env) -> None:
    tc, storage, user_id, referral, backend = client_env
    tc.post(
        f"/referrals/{referral.id}/attachments",
        data={"kind": "lab", "label": "Report"},
        files={"file": ("x.pdf", _PDF_BYTES, "application/pdf")},
        follow_redirects=False,
    )
    scope = Scope(user_id=user_id)
    [row] = [r for r in storage.list_referral_attachments(scope, referral.id) if r.storage_ref]
    storage_ref = row.storage_ref
    assert backend._has(storage_ref)

    resp = tc.delete(f"/attachments/{row.id}")
    assert resp.status_code == 204
    # DB row gone + bucket purged.
    assert storage.get_referral_attachment(scope, row.id) is None
    assert not backend._has(storage_ref)


def test_upload_rollback_on_backend_failure(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A StorageFileError during put must delete the placeholder DB row."""
    from docstats.storage_files.base import StorageFileError

    reset_memory_singleton_for_tests()
    monkeypatch.setenv("ATTACHMENT_UPLOAD_ENABLED", "1")
    user_id = storage.create_user("u@example.com", "hashed")
    storage.record_phi_consent(
        user_id=user_id,
        phi_consent_version=CURRENT_PHI_CONSENT_VERSION,
        ip_address="",
        user_agent="",
    )
    _, _, referral, _ = _seed(storage, user_id)

    class _BoomBackend(InMemoryFileBackend):
        async def put(self, *, path, data, mime_type):
            raise StorageFileError("simulated bucket outage")

    backend = _BoomBackend()
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_current_user] = lambda: _fake_user(user_id)
    app.dependency_overrides[get_file_backend] = lambda: backend

    try:
        tc = TestClient(app)
        before = len(storage.list_referral_attachments(Scope(user_id=user_id), referral.id))
        resp = tc.post(
            f"/referrals/{referral.id}/attachments",
            data={"kind": "lab", "label": "x"},
            files={"file": ("x.pdf", _PDF_BYTES, "application/pdf")},
            follow_redirects=False,
        )
        assert resp.status_code == 502
        # No placeholder row left behind.
        after = len(storage.list_referral_attachments(Scope(user_id=user_id), referral.id))
        assert after == before
    finally:
        app.dependency_overrides.clear()
