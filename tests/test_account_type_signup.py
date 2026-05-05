"""Route-level tests for the patient/clinician signup branching."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from docstats.client import NPPESClient
from docstats.domain.identity import ClinicianVerification
from docstats.routes._common import get_client, get_oig_client
from docstats.storage import Storage, get_storage
from docstats.web import app

GOOD_NPI = "1234567893"


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    return Storage(db_path=tmp_path / "signup.db")


@pytest.fixture
def client(storage: Storage, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """TestClient with NPPES + OIG dep-overridden so verify_clinician runs deterministically.

    The verify_clinician pipeline is exercised in
    tests/domain/test_clinician_verification.py — these tests assert
    the route behavior given a known verdict, so we patch the call to
    verify_clinician at module level. This keeps each signup test
    independent of NPPES network behavior and avoids fragile fixture
    construction for an NPIResult-shaped MagicMock.
    """
    app.dependency_overrides[get_storage] = lambda: storage
    nppes = MagicMock(spec=NPPESClient)
    app.dependency_overrides[get_client] = lambda: nppes
    app.dependency_overrides[get_oig_client] = lambda: None  # treated as oig_unavailable
    yield TestClient(app)
    app.dependency_overrides.clear()


def _patch_verify(monkeypatch, verdict: str, reasons: list[str] | None = None) -> None:
    """Replace ``docstats.routes.auth.verify_clinician`` with a fixed verdict."""
    from docstats.routes import auth as auth_routes

    def fake(**_kw):
        return ClinicianVerification(
            verdict=verdict,  # type: ignore[arg-type]
            reasons=reasons or [],
            primary_taxonomy="207RC0000X" if verdict == "verified" else None,
            method="test_stub",
        )

    monkeypatch.setattr(auth_routes, "verify_clinician", fake)


# ─── Patient signup ───────────────────────────────────────────


def test_patient_signup_creates_account_type_patient(client: TestClient, storage: Storage) -> None:
    resp = client.post(
        "/auth/signup",
        data={
            "email": "alice@example.com",
            "password": "longenoughpw",
            "confirm_password": "longenoughpw",
            "account_type": "patient",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/onboarding"

    row = storage.get_user_by_email("alice@example.com")
    assert row is not None
    assert row["account_type"] == "patient"
    assert row["clinician_verification_status"] == "not_applicable"
    assert row["individual_npi"] is None


def test_patient_signup_default_when_account_type_omitted(
    client: TestClient, storage: Storage
) -> None:
    resp = client.post(
        "/auth/signup",
        data={
            "email": "bob@example.com",
            "password": "longenoughpw",
            "confirm_password": "longenoughpw",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    row = storage.get_user_by_email("bob@example.com")
    assert row is not None
    assert row["account_type"] == "patient"


# ─── Clinician signup ─────────────────────────────────────────


def test_clinician_signup_requires_first_last_name(
    client: TestClient, storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_verify(monkeypatch, "verified")
    resp = client.post(
        "/auth/signup",
        data={
            "email": "doc@example.com",
            "password": "longenoughpw",
            "confirm_password": "longenoughpw",
            "account_type": "clinician",
            "individual_npi": GOOD_NPI,
            "state_license_state": "TX",
            "attestation": "on",
        },
    )
    assert resp.status_code == 200
    assert "First and last name are required" in resp.text
    assert storage.get_user_by_email("doc@example.com") is None


def test_clinician_signup_requires_attestation(
    client: TestClient, storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_verify(monkeypatch, "verified")
    resp = client.post(
        "/auth/signup",
        data={
            "email": "doc@example.com",
            "password": "longenoughpw",
            "confirm_password": "longenoughpw",
            "account_type": "clinician",
            "first_name": "Steven",
            "last_name": "Dennis",
            "individual_npi": GOOD_NPI,
            "state_license_state": "TX",
            # attestation missing
        },
    )
    assert resp.status_code == 200
    assert "attest" in resp.text.lower()


def test_clinician_signup_verified_persists_all_fields(
    client: TestClient, storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_verify(monkeypatch, "verified")
    resp = client.post(
        "/auth/signup",
        data={
            "email": "doc@example.com",
            "password": "longenoughpw",
            "confirm_password": "longenoughpw",
            "account_type": "clinician",
            "first_name": "Steven",
            "last_name": "Dennis",
            "individual_npi": GOOD_NPI,
            "state_license_state": "TX",
            "state_license_number": "A-12345",
            "credentials": "MD, FAAFP",
            "attestation": "on",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    row = storage.get_user_by_email("doc@example.com")
    assert row is not None
    assert row["account_type"] == "clinician"
    assert row["clinician_verification_status"] == "verified"
    assert row["individual_npi"] == GOOD_NPI
    assert row["first_name"] == "Steven"
    assert row["last_name"] == "Dennis"
    assert row["credentials"] == "MD, FAAFP"
    assert row["state_license_number"] == "A-12345"
    assert row["state_license_state"] == "TX"
    assert row["clinician_verified_method"] == "test_stub"


def test_clinician_signup_pending_review_creates_user(
    client: TestClient, storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_verify(monkeypatch, "pending_review", reasons=["name_mismatch"])
    resp = client.post(
        "/auth/signup",
        data={
            "email": "doc@example.com",
            "password": "longenoughpw",
            "confirm_password": "longenoughpw",
            "account_type": "clinician",
            "first_name": "Mismatch",
            "last_name": "Name",
            "individual_npi": GOOD_NPI,
            "state_license_state": "TX",
            "attestation": "on",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    row = storage.get_user_by_email("doc@example.com")
    assert row is not None
    assert row["clinician_verification_status"] == "pending_review"
    assert row["clinician_verification_reasons"] == ["name_mismatch"]


def test_clinician_signup_rejected_creates_no_user_and_returns_generic(
    client: TestClient, storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_verify(monkeypatch, "rejected", reasons=["oig_excluded"])
    resp = client.post(
        "/auth/signup",
        data={
            "email": "doc@example.com",
            "password": "longenoughpw",
            "confirm_password": "longenoughpw",
            "account_type": "clinician",
            "first_name": "Steven",
            "last_name": "Dennis",
            "individual_npi": GOOD_NPI,
            "state_license_state": "TX",
            "attestation": "on",
        },
    )
    assert resp.status_code == 200
    # Generic message — never confirms OIG exclusion specifically.
    # Lowercase HTML, ignoring autoescape variants of the apostrophe.
    body = resp.text.lower()
    assert "verify your clinician credentials" in body
    assert "oig" not in body
    assert "exclud" not in body
    assert storage.get_user_by_email("doc@example.com") is None


# ─── Audience picker template ─────────────────────────────────


def test_signup_get_renders_audience_picker(client: TestClient) -> None:
    resp = client.get("/auth/signup")
    assert resp.status_code == 200
    body = resp.text
    assert "I'm a patient" in body
    assert "I'm a healthcare provider" in body


def test_signup_get_with_clinician_query_renders_clinician_tab(client: TestClient) -> None:
    resp = client.get("/auth/signup?type=clinician")
    assert resp.status_code == 200
    # The clinician tab should be active and the NPI field rendered.
    assert "individual_npi" in resp.text
