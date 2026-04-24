"""Phase 10.D — packet embedding of real attachment bytes.

Tests cover:
- ``fetch_attachment_pdfs`` returns PDFs, skips non-PDFs, handles missing blobs
- ``build_delivery_packet`` concatenates parts, handles attachment_pdfs
- Export route's ``attachment_pdfs`` include token (accepted + spliced)
- Export preview page exposes the checkbox only when attachments exist
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytest.importorskip("weasyprint", reason="weasyprint system libs not installed")

from fastapi.testclient import TestClient  # noqa: E402

from docstats.auth import get_current_user  # noqa: E402
from docstats.delivery.packet_builder import build_delivery_packet  # noqa: E402
from docstats.exports.pdf import fetch_attachment_pdfs  # noqa: E402
from docstats.phi import CURRENT_PHI_CONSENT_VERSION  # noqa: E402
from docstats.scope import Scope  # noqa: E402
from docstats.storage import Storage, get_storage  # noqa: E402
from docstats.storage_files import InMemoryFileBackend  # noqa: E402
from docstats.storage_files.factory import (  # noqa: E402
    get_file_backend,
    reset_memory_singleton_for_tests,
)
from docstats.web import app  # noqa: E402


# A minimum-viable PDF (single blank page) — small enough to embed in tests
# but a real PdfReader parses it.  Hand-built so we don't bundle a binary
# fixture with the repo.
_MINI_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Count 1/Kids[3 0 R]>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R>>endobj\n"
    b"4 0 obj<</Length 8>>stream\nBT ET\nendstream\nendobj\n"
    b"xref\n0 5\n"
    b"0000000000 65535 f \n"
    b"0000000009 00000 n \n"
    b"0000000055 00000 n \n"
    b"0000000106 00000 n \n"
    b"0000000184 00000 n \n"
    b"trailer<</Size 5/Root 1 0 R>>\n"
    b"startxref\n233\n%%EOF"
)


# ---------- Fixtures ----------


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    return Storage(db_path=tmp_path / "test.db")


def _seed(storage: Storage, user_id: int):
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
    return scope, patient, referral


# ---------- fetch_attachment_pdfs ----------


def test_fetch_returns_empty_when_no_attachments(storage: Storage) -> None:
    uid = storage.create_user("u@example.com", "h")
    scope, _, referral = _seed(storage, uid)
    backend = InMemoryFileBackend()

    async def go():
        return await fetch_attachment_pdfs(
            storage=storage, scope=scope, referral=referral, file_backend=backend
        )

    assert asyncio.run(go()) == []


def test_fetch_returns_pdf_bytes(storage: Storage) -> None:
    uid = storage.create_user("u@example.com", "h")
    scope, _, referral = _seed(storage, uid)
    backend = InMemoryFileBackend()

    # Seed a PDF-backed attachment.
    att = storage.add_referral_attachment(
        scope,
        referral.id,
        kind="lab",
        label="Lab",
        storage_ref="user-1/1/42.pdf",
        checklist_only=False,
    )
    asyncio.run(backend.put(path="user-1/1/42.pdf", data=_MINI_PDF, mime_type="application/pdf"))

    async def go():
        return await fetch_attachment_pdfs(
            storage=storage, scope=scope, referral=referral, file_backend=backend
        )

    result = asyncio.run(go())
    assert len(result) == 1
    aid, data = result[0]
    assert aid == att.id
    assert data == _MINI_PDF


def test_fetch_skips_non_pdf_attachments(storage: Storage) -> None:
    """Images + DOCX are in the checklist, not in the PDF concat stream."""
    uid = storage.create_user("u@example.com", "h")
    scope, _, referral = _seed(storage, uid)
    backend = InMemoryFileBackend()
    storage.add_referral_attachment(
        scope,
        referral.id,
        kind="imaging",
        label="X-ray",
        storage_ref="user-1/1/1.png",
        checklist_only=False,
    )
    asyncio.run(backend.put(path="user-1/1/1.png", data=b"\x89PNG", mime_type="image/png"))

    result = asyncio.run(
        fetch_attachment_pdfs(storage=storage, scope=scope, referral=referral, file_backend=backend)
    )
    assert result == []


def test_fetch_skips_checklist_only_rows(storage: Storage) -> None:
    uid = storage.create_user("u@example.com", "h")
    scope, _, referral = _seed(storage, uid)
    backend = InMemoryFileBackend()
    storage.add_referral_attachment(
        scope,
        referral.id,
        kind="lab",
        label="Pending",
        storage_ref=None,  # pre-10.A placeholder
        checklist_only=True,
    )
    assert (
        asyncio.run(
            fetch_attachment_pdfs(
                storage=storage, scope=scope, referral=referral, file_backend=backend
            )
        )
        == []
    )


def test_fetch_logs_and_skips_missing_blob(storage: Storage) -> None:
    """If the DB says the blob exists but the bucket says 404, we keep
    going with a warning — the packet still renders without this piece."""
    uid = storage.create_user("u@example.com", "h")
    scope, _, referral = _seed(storage, uid)
    backend = InMemoryFileBackend()
    storage.add_referral_attachment(
        scope,
        referral.id,
        kind="lab",
        label="Orphaned",
        storage_ref="user-1/1/orphan.pdf",
        checklist_only=False,
    )
    # Deliberately don't put bytes in the backend.
    result = asyncio.run(
        fetch_attachment_pdfs(storage=storage, scope=scope, referral=referral, file_backend=backend)
    )
    assert result == []


# ---------- build_delivery_packet ----------


def _seed_delivery(storage: Storage, scope: Scope, referral_id: int, **kwargs):
    return storage.create_delivery(
        scope,
        referral_id=referral_id,
        channel=kwargs.get("channel", "fax"),
        recipient=kwargs.get("recipient", "+15555551234"),
        packet_artifact=kwargs.get("packet_artifact", {}),
    )


def test_build_packet_raises_on_missing_referral(storage: Storage) -> None:
    uid = storage.create_user("u@example.com", "h")
    scope, _, referral = _seed(storage, uid)
    delivery = _seed_delivery(storage, scope, referral.id)
    # Soft-delete the referral — packet build must fail fast.
    storage.soft_delete_referral(scope, referral.id)

    backend = InMemoryFileBackend()
    with pytest.raises(ValueError, match="not found"):
        asyncio.run(build_delivery_packet(storage, backend, delivery))


def test_build_packet_raises_on_missing_patient(storage: Storage) -> None:
    uid = storage.create_user("u@example.com", "h")
    scope, patient, referral = _seed(storage, uid)
    delivery = _seed_delivery(storage, scope, referral.id)
    storage.soft_delete_patient(scope, patient.id)

    backend = InMemoryFileBackend()
    with pytest.raises(ValueError, match="Patient"):
        asyncio.run(build_delivery_packet(storage, backend, delivery))


def test_build_packet_default_include(storage: Storage) -> None:
    """Empty packet_artifact → default include → non-empty PDF."""
    uid = storage.create_user("u@example.com", "h")
    scope, _, referral = _seed(storage, uid)
    delivery = _seed_delivery(storage, scope, referral.id)
    backend = InMemoryFileBackend()
    out = asyncio.run(build_delivery_packet(storage, backend, delivery))
    assert out.startswith(b"%PDF")


def test_build_packet_with_attachment_pdfs(storage: Storage) -> None:
    uid = storage.create_user("u@example.com", "h")
    scope, _, referral = _seed(storage, uid)
    storage.add_referral_attachment(
        scope,
        referral.id,
        kind="lab",
        label="Lab",
        storage_ref="u/1/1.pdf",
        checklist_only=False,
    )
    backend = InMemoryFileBackend()
    asyncio.run(backend.put(path="u/1/1.pdf", data=_MINI_PDF, mime_type="application/pdf"))

    delivery = _seed_delivery(
        storage,
        scope,
        referral.id,
        packet_artifact={"include": ["summary", "attachment_pdfs"]},
    )
    out = asyncio.run(build_delivery_packet(storage, backend, delivery))
    # Should contain at least 2 pages (summary + our mini-pdf).
    from io import BytesIO

    from pypdf import PdfReader

    reader = PdfReader(BytesIO(out))
    assert len(reader.pages) >= 2


def test_build_packet_drops_unknown_artifact_tokens(storage: Storage) -> None:
    uid = storage.create_user("u@example.com", "h")
    scope, _, referral = _seed(storage, uid)
    delivery = _seed_delivery(
        storage,
        scope,
        referral.id,
        packet_artifact={"include": ["summary", "pigeon", "packet", "fax_cover"]},
    )
    backend = InMemoryFileBackend()
    # Defense-in-depth: unknown tokens silently dropped; nested ``packet``
    # also dropped.  Result renders without error.
    out = asyncio.run(build_delivery_packet(storage, backend, delivery))
    assert out.startswith(b"%PDF")


def test_build_packet_empty_include_raises(storage: Storage) -> None:
    """All-junk include list → ValueError (no renderable parts)."""
    uid = storage.create_user("u@example.com", "h")
    scope, _, referral = _seed(storage, uid)
    # ``_parse_include`` with all-junk input falls back to the default
    # include list, so we inject a list that survives parsing but whose
    # only valid token is ``attachment_pdfs`` with no attachments to
    # embed — that path produces 0 parts.
    delivery = _seed_delivery(
        storage,
        scope,
        referral.id,
        packet_artifact={"include": ["attachment_pdfs"]},
    )
    backend = InMemoryFileBackend()
    with pytest.raises(ValueError, match="empty"):
        asyncio.run(build_delivery_packet(storage, backend, delivery))


# ---------- Route: ?include=attachment_pdfs ----------


def _fake_user(user_id: int) -> dict:
    return {
        "id": user_id,
        "email": "u@example.com",
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


def test_export_route_accepts_attachment_pdfs_include(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    reset_memory_singleton_for_tests()
    uid = storage.create_user("u@example.com", "h")
    storage.record_phi_consent(
        user_id=uid,
        phi_consent_version=CURRENT_PHI_CONSENT_VERSION,
        ip_address="",
        user_agent="",
    )
    scope, _, referral = _seed(storage, uid)
    storage.add_referral_attachment(
        scope,
        referral.id,
        kind="lab",
        label="Lab",
        storage_ref="u/1/1.pdf",
        checklist_only=False,
    )
    backend = InMemoryFileBackend()
    asyncio.run(backend.put(path="u/1/1.pdf", data=_MINI_PDF, mime_type="application/pdf"))

    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_current_user] = lambda: _fake_user(uid)
    app.dependency_overrides[get_file_backend] = lambda: backend
    try:
        tc = TestClient(app)
        resp = tc.get(
            f"/referrals/{referral.id}/export.pdf?artifact=packet&include=summary,attachment_pdfs"
        )
        assert resp.status_code == 200
        assert resp.content.startswith(b"%PDF")
    finally:
        app.dependency_overrides.clear()


def test_export_route_rejects_unknown_include_token(
    storage: Storage,
) -> None:
    """Tokens that aren't artifacts AND aren't ``attachment_pdfs`` still 400."""
    reset_memory_singleton_for_tests()
    uid = storage.create_user("u@example.com", "h")
    storage.record_phi_consent(
        user_id=uid,
        phi_consent_version=CURRENT_PHI_CONSENT_VERSION,
        ip_address="",
        user_agent="",
    )
    scope, _, referral = _seed(storage, uid)
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_current_user] = lambda: _fake_user(uid)
    try:
        tc = TestClient(app)
        resp = tc.get(f"/referrals/{referral.id}/export.pdf?artifact=packet&include=summary,pigeon")
        assert resp.status_code == 400
    finally:
        app.dependency_overrides.clear()


def test_export_preview_shows_attachment_pdfs_checkbox_only_when_attachments_exist(
    storage: Storage,
) -> None:
    reset_memory_singleton_for_tests()
    uid = storage.create_user("u@example.com", "h")
    storage.record_phi_consent(
        user_id=uid,
        phi_consent_version=CURRENT_PHI_CONSENT_VERSION,
        ip_address="",
        user_agent="",
    )
    scope, _, referral = _seed(storage, uid)

    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_current_user] = lambda: _fake_user(uid)
    try:
        tc = TestClient(app)
        # No attachments yet → toggle hidden.
        resp = tc.get(f"/referrals/{referral.id}/export")
        assert resp.status_code == 200
        assert "attachment_pdfs" not in resp.text

        # Add a bucket-backed attachment → toggle appears.
        storage.add_referral_attachment(
            scope,
            referral.id,
            kind="lab",
            label="x",
            storage_ref="u/1/1.pdf",
            checklist_only=False,
        )
        resp = tc.get(f"/referrals/{referral.id}/export")
        assert resp.status_code == 200
        assert "attachment_pdfs" in resp.text
        assert "Attachment PDFs (embedded)" in resp.text
    finally:
        app.dependency_overrides.clear()


def test_export_preview_hides_toggle_when_only_checklist_only(
    storage: Storage,
) -> None:
    """Checklist-only rows (no storage_ref) should NOT trigger the toggle."""
    reset_memory_singleton_for_tests()
    uid = storage.create_user("u@example.com", "h")
    storage.record_phi_consent(
        user_id=uid,
        phi_consent_version=CURRENT_PHI_CONSENT_VERSION,
        ip_address="",
        user_agent="",
    )
    scope, _, referral = _seed(storage, uid)
    storage.add_referral_attachment(
        scope,
        referral.id,
        kind="lab",
        label="Pending",
        storage_ref=None,
        checklist_only=True,
    )
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_current_user] = lambda: _fake_user(uid)
    try:
        tc = TestClient(app)
        resp = tc.get(f"/referrals/{referral.id}/export")
        assert resp.status_code == 200
        assert "attachment_pdfs" not in resp.text
    finally:
        app.dependency_overrides.clear()
