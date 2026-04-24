"""Phase 10.B — Virus scanner adapter + upload integration tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from docstats.auth import get_current_user
from docstats.phi import CURRENT_PHI_CONSENT_VERSION
from docstats.scope import Scope
from docstats.storage import Storage, get_storage
from docstats.storage_files import (
    InMemoryFileBackend,
    ScannerUnavailable,
    ScanResult,
)
from docstats.storage_files.factory import (
    get_file_backend,
    reset_memory_singleton_for_tests,
)
from docstats.storage_files.scanner_factory import (
    get_virus_scanner,
    virus_scan_is_required,
)
from docstats.storage_files.scanners.cloudmersive import CloudmersiveVirusScanner
from docstats.storage_files.scanners.noop import NoOpVirusScanner
from docstats.web import app


_PDF = b"%PDF-1.4\n<fake>"


# ---------- Scanner factory ----------


def test_factory_noop_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIRUS_SCANNER_BACKEND", "noop")
    s = get_virus_scanner()
    assert isinstance(s, NoOpVirusScanner)


def test_factory_none_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIRUS_SCANNER_BACKEND", "none")
    assert get_virus_scanner() is None


def test_factory_cloudmersive_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIRUS_SCANNER_BACKEND", "cloudmersive")
    monkeypatch.setenv("CLOUDMERSIVE_API_KEY", "test_key_abc")
    s = get_virus_scanner()
    assert isinstance(s, CloudmersiveVirusScanner)


def test_factory_auto_noop_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VIRUS_SCANNER_BACKEND", raising=False)
    monkeypatch.delenv("CLOUDMERSIVE_API_KEY", raising=False)
    s = get_virus_scanner()
    assert isinstance(s, NoOpVirusScanner)


def test_factory_auto_cloudmersive_with_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VIRUS_SCANNER_BACKEND", raising=False)
    monkeypatch.setenv("CLOUDMERSIVE_API_KEY", "test_key_abc")
    s = get_virus_scanner()
    assert isinstance(s, CloudmersiveVirusScanner)


def test_virus_scan_required_default_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VIRUS_SCAN_REQUIRED", raising=False)
    assert virus_scan_is_required() is False


@pytest.mark.parametrize("val", ["1", "true", "True", "yes"])
def test_virus_scan_required_truthy_values(monkeypatch: pytest.MonkeyPatch, val: str) -> None:
    monkeypatch.setenv("VIRUS_SCAN_REQUIRED", val)
    assert virus_scan_is_required() is True


@pytest.mark.parametrize("val", ["0", "false", "no", ""])
def test_virus_scan_required_falsy_values(monkeypatch: pytest.MonkeyPatch, val: str) -> None:
    monkeypatch.setenv("VIRUS_SCAN_REQUIRED", val)
    assert virus_scan_is_required() is False


# ---------- Cloudmersive adapter ----------


def test_cloudmersive_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLOUDMERSIVE_API_KEY", raising=False)
    with pytest.raises(ScannerUnavailable, match="CLOUDMERSIVE_API_KEY"):
        CloudmersiveVirusScanner()


def test_cloudmersive_instantiates_with_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLOUDMERSIVE_API_KEY", "k")
    s = CloudmersiveVirusScanner()
    assert s.name == "cloudmersive"


def _resp(status: int, json_body: dict | None = None, text: str = "") -> httpx.Response:
    req = httpx.Request("POST", "https://api.cloudmersive.com/virus/scan/file")
    if json_body is not None:
        return httpx.Response(status, json=json_body, request=req)
    return httpx.Response(status, text=text, request=req)


def _run(coro):
    return asyncio.run(coro)


def test_cloudmersive_clean_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLOUDMERSIVE_API_KEY", "k")
    s = CloudmersiveVirusScanner()
    resp = _resp(200, {"CleanResult": True})
    with patch.object(httpx.AsyncClient, "post", new=AsyncMock(return_value=resp)):
        result = _run(s.scan(_PDF, filename="x.pdf"))
    assert result.infected is False
    assert result.scanner_name == "cloudmersive"
    assert result.threat_names == []


def test_cloudmersive_infected_result_extracts_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLOUDMERSIVE_API_KEY", "k")
    s = CloudmersiveVirusScanner()
    resp = _resp(
        200,
        {
            "CleanResult": False,
            "FoundViruses": [
                {"FileName": "x.pdf", "VirusName": "EICAR-Test-File"},
                {"FileName": "x.pdf", "VirusName": "Trojan.Generic"},
            ],
        },
    )
    with patch.object(httpx.AsyncClient, "post", new=AsyncMock(return_value=resp)):
        result = _run(s.scan(_PDF))
    assert result.infected is True
    assert result.threat_names == ["EICAR-Test-File", "Trojan.Generic"]


def test_cloudmersive_infected_without_virus_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """Some Cloudmersive responses return CleanResult=False with no virus list."""
    monkeypatch.setenv("CLOUDMERSIVE_API_KEY", "k")
    s = CloudmersiveVirusScanner()
    resp = _resp(200, {"CleanResult": False})
    with patch.object(httpx.AsyncClient, "post", new=AsyncMock(return_value=resp)):
        result = _run(s.scan(_PDF))
    assert result.infected is True
    assert result.threat_names == []


def test_cloudmersive_401_maps_to_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLOUDMERSIVE_API_KEY", "wrong")
    s = CloudmersiveVirusScanner()
    resp = _resp(401, text="bad key")
    with patch.object(httpx.AsyncClient, "post", new=AsyncMock(return_value=resp)):
        with pytest.raises(ScannerUnavailable, match="401"):
            _run(s.scan(_PDF))


def test_cloudmersive_429_maps_to_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLOUDMERSIVE_API_KEY", "k")
    s = CloudmersiveVirusScanner()
    with patch.object(httpx.AsyncClient, "post", new=AsyncMock(return_value=_resp(429))):
        with pytest.raises(ScannerUnavailable, match="429"):
            _run(s.scan(_PDF))


def test_cloudmersive_5xx_maps_to_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLOUDMERSIVE_API_KEY", "k")
    s = CloudmersiveVirusScanner()
    with patch.object(httpx.AsyncClient, "post", new=AsyncMock(return_value=_resp(503))):
        with pytest.raises(ScannerUnavailable):
            _run(s.scan(_PDF))


def test_cloudmersive_timeout_maps_to_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLOUDMERSIVE_API_KEY", "k")
    s = CloudmersiveVirusScanner()

    async def _boom(*a, **kw):
        raise httpx.TimeoutException("slow")

    with patch.object(httpx.AsyncClient, "post", new=AsyncMock(side_effect=_boom)):
        with pytest.raises(ScannerUnavailable, match="timeout"):
            _run(s.scan(_PDF))


def test_cloudmersive_missing_clean_result_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLOUDMERSIVE_API_KEY", "k")
    s = CloudmersiveVirusScanner()
    resp = _resp(200, {"wat": "?"})  # no CleanResult
    with patch.object(httpx.AsyncClient, "post", new=AsyncMock(return_value=resp)):
        with pytest.raises(ScannerUnavailable, match="CleanResult"):
            _run(s.scan(_PDF))


def test_cloudmersive_empty_input_returns_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defensive: empty bytes skip the network call and report clean."""
    monkeypatch.setenv("CLOUDMERSIVE_API_KEY", "k")
    s = CloudmersiveVirusScanner()
    # No patch.object — if the network is called with empty bytes, that's a bug.
    result = _run(s.scan(b""))
    assert result.infected is False


def test_cloudmersive_sends_apikey_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLOUDMERSIVE_API_KEY", "super_secret")
    s = CloudmersiveVirusScanner()
    resp = _resp(200, {"CleanResult": True})
    with patch.object(httpx.AsyncClient, "post", new=AsyncMock(return_value=resp)) as p:
        _run(s.scan(_PDF, filename="x.pdf"))
    call = p.call_args
    assert call.kwargs["headers"]["Apikey"] == "super_secret"
    # Multipart field name must match Cloudmersive's REST API (inputFile,
    # camelCase — NOT input_file which is the Python SDK symbol).
    assert "inputFile" in call.kwargs["files"]


# ---------- NoOp scanner ----------


def test_noop_scanner_is_always_clean() -> None:
    s = NoOpVirusScanner()
    result = _run(s.scan(b"anything"))
    assert result.infected is False
    assert result.scanner_name == "noop"


# ---------- Route integration ----------


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


def _seed(storage: Storage, user_id: int) -> int:
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
    return referral.id


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    return Storage(db_path=tmp_path / "test.db")


class _InfectedScanner:
    name = "stub-infected"

    async def scan(self, data, *, filename=None):
        return ScanResult(
            infected=True,
            scanner_name=self.name,
            threat_names=["EICAR-Test-File"],
        )


class _CleanScanner:
    name = "stub-clean"

    async def scan(self, data, *, filename=None):
        return ScanResult(infected=False, scanner_name=self.name, threat_names=[])


class _UnavailableScanner:
    name = "stub-broken"

    async def scan(self, data, *, filename=None):
        raise ScannerUnavailable("simulated vendor outage")


def _env(storage: Storage, user_id: int, referral_id: int, scanner, backend=None):
    backend = backend or InMemoryFileBackend()
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_current_user] = lambda: _fake_user(user_id)
    app.dependency_overrides[get_file_backend] = lambda: backend
    app.dependency_overrides[get_virus_scanner] = lambda: scanner
    return backend


def test_upload_rejects_infected_file(storage: Storage, monkeypatch: pytest.MonkeyPatch) -> None:
    reset_memory_singleton_for_tests()
    monkeypatch.setenv("ATTACHMENT_UPLOAD_ENABLED", "1")
    user_id = storage.create_user("u@example.com", "hashed")
    storage.record_phi_consent(
        user_id=user_id,
        phi_consent_version=CURRENT_PHI_CONSENT_VERSION,
        ip_address="",
        user_agent="",
    )
    referral_id = _seed(storage, user_id)
    backend = _env(storage, user_id, referral_id, _InfectedScanner())
    try:
        tc = TestClient(app)
        resp = tc.post(
            f"/referrals/{referral_id}/attachments",
            data={"kind": "lab", "label": "bad"},
            files={"file": ("x.pdf", _PDF, "application/pdf")},
            follow_redirects=False,
        )
        assert resp.status_code == 422
        assert "virus scan" in resp.text.lower()
        assert "eicar" in resp.text.lower()
        # No DB row; no bucket write.
        assert storage.list_referral_attachments(Scope(user_id=user_id), referral_id) == []
        assert backend._size() == 0
        # Audit row recorded.
        events = storage.list_audit_events(scope_user_id=user_id, action="attachment.scan_rejected")
        assert len(events) == 1
    finally:
        app.dependency_overrides.clear()


def test_upload_clean_file_proceeds_and_records_scanner(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    reset_memory_singleton_for_tests()
    monkeypatch.setenv("ATTACHMENT_UPLOAD_ENABLED", "1")
    user_id = storage.create_user("u@example.com", "hashed")
    storage.record_phi_consent(
        user_id=user_id,
        phi_consent_version=CURRENT_PHI_CONSENT_VERSION,
        ip_address="",
        user_agent="",
    )
    referral_id = _seed(storage, user_id)
    backend = _env(storage, user_id, referral_id, _CleanScanner())
    try:
        tc = TestClient(app)
        resp = tc.post(
            f"/referrals/{referral_id}/attachments",
            data={"kind": "lab", "label": "fine"},
            files={"file": ("x.pdf", _PDF, "application/pdf")},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        events = storage.list_audit_events(scope_user_id=user_id, action="attachment.create")
        assert len(events) == 1
        # Scanner name threaded into the audit metadata.
        import json as _json

        raw = events[0].metadata or {}
        if isinstance(raw, str):
            raw = _json.loads(raw)
        assert raw["scanner"] == "stub-clean"
        assert backend._size() == 1
    finally:
        app.dependency_overrides.clear()


def test_upload_scanner_unavailable_with_required_rejects_502(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    reset_memory_singleton_for_tests()
    monkeypatch.setenv("ATTACHMENT_UPLOAD_ENABLED", "1")
    monkeypatch.setenv("VIRUS_SCAN_REQUIRED", "1")
    user_id = storage.create_user("u@example.com", "hashed")
    storage.record_phi_consent(
        user_id=user_id,
        phi_consent_version=CURRENT_PHI_CONSENT_VERSION,
        ip_address="",
        user_agent="",
    )
    referral_id = _seed(storage, user_id)
    backend = _env(storage, user_id, referral_id, _UnavailableScanner())
    try:
        tc = TestClient(app)
        resp = tc.post(
            f"/referrals/{referral_id}/attachments",
            data={"kind": "lab", "label": "x"},
            files={"file": ("x.pdf", _PDF, "application/pdf")},
            follow_redirects=False,
        )
        assert resp.status_code == 502
        # No DB row, no bucket write.
        assert storage.list_referral_attachments(Scope(user_id=user_id), referral_id) == []
        assert backend._size() == 0
        events = storage.list_audit_events(
            scope_user_id=user_id, action="attachment.scan_unavailable"
        )
        assert len(events) == 1
    finally:
        app.dependency_overrides.clear()


def test_upload_scanner_unavailable_with_not_required_proceeds(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dev mode — scanner breaks, upload succeeds, log records it."""
    reset_memory_singleton_for_tests()
    monkeypatch.setenv("ATTACHMENT_UPLOAD_ENABLED", "1")
    monkeypatch.delenv("VIRUS_SCAN_REQUIRED", raising=False)
    user_id = storage.create_user("u@example.com", "hashed")
    storage.record_phi_consent(
        user_id=user_id,
        phi_consent_version=CURRENT_PHI_CONSENT_VERSION,
        ip_address="",
        user_agent="",
    )
    referral_id = _seed(storage, user_id)
    backend = _env(storage, user_id, referral_id, _UnavailableScanner())
    try:
        tc = TestClient(app)
        resp = tc.post(
            f"/referrals/{referral_id}/attachments",
            data={"kind": "lab", "label": "x"},
            files={"file": ("x.pdf", _PDF, "application/pdf")},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        # Upload landed; scanner name is "none" because no verdict rendered.
        import json as _json

        events = storage.list_audit_events(scope_user_id=user_id, action="attachment.create")
        assert len(events) == 1
        raw = events[0].metadata or {}
        if isinstance(raw, str):
            raw = _json.loads(raw)
        assert raw["scanner"] == "none"
        assert backend._size() == 1
    finally:
        app.dependency_overrides.clear()


def test_upload_no_scanner_configured_with_required_502(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """VIRUS_SCAN_REQUIRED=1 + scanner=None = misconfiguration → 502."""
    reset_memory_singleton_for_tests()
    monkeypatch.setenv("ATTACHMENT_UPLOAD_ENABLED", "1")
    monkeypatch.setenv("VIRUS_SCAN_REQUIRED", "1")
    user_id = storage.create_user("u@example.com", "hashed")
    storage.record_phi_consent(
        user_id=user_id,
        phi_consent_version=CURRENT_PHI_CONSENT_VERSION,
        ip_address="",
        user_agent="",
    )
    referral_id = _seed(storage, user_id)
    backend = _env(storage, user_id, referral_id, None)  # no scanner at all
    try:
        tc = TestClient(app)
        resp = tc.post(
            f"/referrals/{referral_id}/attachments",
            data={"kind": "lab", "label": "x"},
            files={"file": ("x.pdf", _PDF, "application/pdf")},
            follow_redirects=False,
        )
        assert resp.status_code == 502
        assert backend._size() == 0
    finally:
        app.dependency_overrides.clear()
