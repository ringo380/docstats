"""Route-level tests for CSV bulk imports (Phase 4.A)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from docstats.auth import get_current_user
from docstats.phi import CURRENT_PHI_CONSENT_VERSION
from docstats.scope import Scope
from docstats.storage import Storage, get_storage
from docstats.web import app


def _fake_user(user_id: int, *, consent: bool = True) -> dict:
    return {
        "id": user_id,
        "email": "a@example.com",
        "display_name": None,
        "first_name": None,
        "last_name": None,
        "github_id": None,
        "github_login": None,
        "password_hash": "hashed",
        "created_at": "2026-01-01",
        "last_login_at": None,
        "terms_accepted_at": "2026-01-01",
        "phi_consent_at": "2026-01-01" if consent else None,
        "phi_consent_version": CURRENT_PHI_CONSENT_VERSION if consent else None,
        "phi_consent_ip": None,
        "phi_consent_user_agent": None,
        "active_org_id": None,
    }


@pytest.fixture
def solo_client(tmp_path: Path):
    storage = Storage(db_path=tmp_path / "test.db")
    user_id = storage.create_user("a@example.com", "hashed")
    storage.record_phi_consent(
        user_id=user_id,
        phi_consent_version=CURRENT_PHI_CONSENT_VERSION,
        ip_address="",
        user_agent="",
    )
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_current_user] = lambda: _fake_user(user_id)
    yield TestClient(app), storage, user_id
    app.dependency_overrides.clear()


_SAMPLE_CSV = b"""patient_first,patient_last,patient_dob,reason,specialty
Jane,Doe,1980-05-15,Chest pain eval,Cardiology
Bob,Smith,1975-03-02,Knee injury follow-up,Orthopedic Surgery
"""


# --- List view ---


def test_list_empty(solo_client):
    client, _, _ = solo_client
    resp = client.get("/imports")
    assert resp.status_code == 200
    assert "No imports yet" in resp.text


def test_consent_gate(tmp_path: Path):
    storage = Storage(db_path=tmp_path / "test.db")
    user_id = storage.create_user("a@example.com", "hashed")
    # No phi_consent recorded
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_current_user] = lambda: _fake_user(user_id, consent=False)
    try:
        client = TestClient(app)
        resp = client.get("/imports", follow_redirects=False)
        assert resp.status_code in (302, 303, 307)
        assert "/auth/login" in resp.headers.get("location", "")
    finally:
        app.dependency_overrides.clear()


# --- Upload ---


def test_upload_parses_rows(solo_client):
    client, storage, user_id = solo_client
    resp = client.post(
        "/imports",
        files={"file": ("referrals.csv", _SAMPLE_CSV, "text/csv")},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    loc = resp.headers["location"]
    assert loc.startswith("/imports/") and loc.endswith("/map")

    # Storage: csv_import + 2 rows landed.
    scope = Scope(user_id=user_id)
    imports = storage.list_csv_imports(scope)
    assert len(imports) == 1
    imp = imports[0]
    assert imp.row_count == 2
    assert imp.original_filename == "referrals.csv"
    rows = storage.list_csv_import_rows(scope, imp.id)
    assert len(rows) == 2
    assert rows[0].row_index == 1
    assert rows[0].raw_json["patient_first"] == "Jane"
    assert rows[1].raw_json["patient_last"] == "Smith"

    # Audit row
    events = storage.list_audit_events(limit=5)
    assert any(e.action == "import.create" for e in events)


def test_upload_empty_file_rejected(solo_client):
    client, _, _ = solo_client
    resp = client.post(
        "/imports",
        files={"file": ("empty.csv", b"", "text/csv")},
    )
    assert resp.status_code == 422


def test_upload_no_header_rejected(solo_client):
    client, _, _ = solo_client
    # Just bytes, no header row makes DictReader see no fieldnames only if blank
    resp = client.post(
        "/imports",
        files={"file": ("bad.csv", b"\n\n", "text/csv")},
    )
    assert resp.status_code == 422


def test_upload_no_data_rows_rejected(solo_client):
    client, _, _ = solo_client
    resp = client.post(
        "/imports",
        files={"file": ("headers_only.csv", b"a,b,c\n", "text/csv")},
    )
    assert resp.status_code == 422


def test_upload_row_cap_rejected(solo_client):
    """More than MAX_UPLOAD_ROWS rows → 422 at parse time, no row insert."""
    client, storage, user_id = solo_client
    # 2001 data rows after header
    body = b"col\n" + b"x\n" * 2001
    resp = client.post(
        "/imports",
        files={"file": ("big.csv", body, "text/csv")},
    )
    assert resp.status_code == 422
    assert storage.list_csv_imports(Scope(user_id=user_id)) == []


def test_upload_size_cap_rejected(solo_client):
    """> 5 MB payload → 422 before any parsing."""
    client, _, _ = solo_client
    body = b"col\n" + (b"x" * 1024 + b"\n") * 5200  # ~5 MB+
    resp = client.post(
        "/imports",
        files={"file": ("huge.csv", body, "text/csv")},
    )
    assert resp.status_code == 422


def test_upload_non_utf8_rejected(solo_client):
    client, _, _ = solo_client
    # Latin-1 encoded byte that isn't valid UTF-8
    resp = client.post(
        "/imports",
        files={"file": ("latin.csv", b"name\n\xff\xfe\n", "text/csv")},
    )
    assert resp.status_code == 422


def test_upload_bom_handled(solo_client):
    """Excel-saved CSVs often have a UTF-8 BOM on the first header cell."""
    client, storage, user_id = solo_client
    body = b"\xef\xbb\xbfpatient_first,reason\nJane,Eval\n"
    resp = client.post(
        "/imports",
        files={"file": ("bom.csv", body, "text/csv")},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    rows = storage.list_csv_import_rows(
        Scope(user_id=user_id), storage.list_csv_imports(Scope(user_id=user_id))[0].id
    )
    # Header cell should be clean (no \ufeff leak into the key)
    assert "patient_first" in rows[0].raw_json
    assert rows[0].raw_json["patient_first"] == "Jane"


# --- Column mapping (Phase 4.B) ---


def test_map_form_auto_matches_headers(solo_client):
    """Obvious header names (e.g. ``reason``, ``first_name``) should be
    pre-populated in the select dropdowns."""
    client, storage, user_id = solo_client
    client.post(
        "/imports",
        files={
            "file": (
                "r.csv",
                b"first_name,last_name,dob,reason\nJane,Doe,1980-05-15,Eval\n",
                "text/csv",
            )
        },
    )
    imp = storage.list_csv_imports(Scope(user_id=user_id))[0]
    resp = client.get(f"/imports/{imp.id}/map")
    assert resp.status_code == 200
    # Auto-match: first_name → patient_first_name, reason → reason
    assert 'value="patient_first_name" selected' in resp.text
    assert 'value="patient_last_name" selected' in resp.text
    assert 'value="patient_dob" selected' in resp.text
    assert 'value="reason" selected' in resp.text


def test_map_not_found(solo_client):
    client, _, _ = solo_client
    resp = client.get("/imports/99999/map")
    assert resp.status_code == 404


def test_map_save_persists_mapping_and_transitions(solo_client):
    client, storage, user_id = solo_client
    client.post("/imports", files={"file": ("r.csv", _SAMPLE_CSV, "text/csv")})
    imp = storage.list_csv_imports(Scope(user_id=user_id))[0]
    assert imp.status == "uploaded"

    resp = client.post(
        f"/imports/{imp.id}/map",
        data={
            "col__patient_first": "patient_first_name",
            "col__patient_last": "patient_last_name",
            "col__patient_dob": "patient_dob",
            "col__reason": "reason",
            "col__specialty": "specialty_desc",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    updated = storage.get_csv_import(Scope(user_id=user_id), imp.id)
    # Map-save now auto-runs validation (Phase 4.C) — skips the
    # "mapped" intermediate so the user lands on a populated review page.
    assert updated.status == "validated"
    # Stored as {target: csv_header}
    assert updated.mapping["patient_first_name"] == "patient_first"
    assert updated.mapping["reason"] == "reason"
    events = storage.list_audit_events(limit=5)
    assert any(e.action == "import.map" for e in events)
    assert any(e.action == "import.validate" for e in events)


def test_map_save_rejects_unknown_target(solo_client):
    client, storage, user_id = solo_client
    client.post("/imports", files={"file": ("r.csv", _SAMPLE_CSV, "text/csv")})
    imp = storage.list_csv_imports(Scope(user_id=user_id))[0]
    resp = client.post(
        f"/imports/{imp.id}/map",
        data={"col__patient_first": "NOT_A_FIELD"},
    )
    assert resp.status_code == 422


def test_map_save_rejects_duplicate_target(solo_client):
    """Two CSV columns mapped to the same target field → 422."""
    client, storage, user_id = solo_client
    client.post("/imports", files={"file": ("r.csv", _SAMPLE_CSV, "text/csv")})
    imp = storage.list_csv_imports(Scope(user_id=user_id))[0]
    resp = client.post(
        f"/imports/{imp.id}/map",
        data={
            "col__patient_first": "reason",
            "col__patient_last": "reason",  # collision
        },
    )
    assert resp.status_code == 422


def test_map_save_ignores_unmapped_blank(solo_client):
    """Blank string for a column means 'skip' — doesn't land in the mapping."""
    client, storage, user_id = solo_client
    client.post("/imports", files={"file": ("r.csv", _SAMPLE_CSV, "text/csv")})
    imp = storage.list_csv_imports(Scope(user_id=user_id))[0]
    resp = client.post(
        f"/imports/{imp.id}/map",
        data={
            "col__patient_first": "patient_first_name",
            "col__patient_last": "",  # explicit skip
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    updated = storage.get_csv_import(Scope(user_id=user_id), imp.id)
    assert "patient_first_name" in updated.mapping
    assert updated.mapping.get("patient_last_name") is None


# --- Validation + review (Phase 4.C) ---


_SAMPLE_MAPPING = {
    "col__patient_first": "patient_first_name",
    "col__patient_last": "patient_last_name",
    "col__patient_dob": "patient_dob",
    "col__reason": "reason",
    "col__specialty": "specialty_desc",
}


def _seed_mapped_import(client, storage, user_id, csv: bytes = _SAMPLE_CSV):
    client.post("/imports", files={"file": ("r.csv", csv, "text/csv")})
    imp = storage.list_csv_imports(Scope(user_id=user_id))[0]
    client.post(f"/imports/{imp.id}/map", data=_SAMPLE_MAPPING)
    return storage.get_csv_import(Scope(user_id=user_id), imp.id)


def test_map_save_auto_validates(solo_client):
    """Saving the mapping runs validators + transitions to 'validated' in
    one round-trip. Review page renders with real counts."""
    client, storage, user_id = solo_client
    imp = _seed_mapped_import(client, storage, user_id)
    assert imp.status == "validated"
    # Both rows valid (reason + names + specialty present)
    assert imp.error_report["valid"] == 2
    assert imp.error_report["error"] == 0
    rows = storage.list_csv_import_rows(Scope(user_id=user_id), imp.id)
    assert all(r.status == "valid" for r in rows)


def test_validation_flags_missing_reason(solo_client):
    client, storage, user_id = solo_client
    csv_body = (
        b"patient_first,patient_last,patient_dob,reason,specialty\n"
        b"Jane,Doe,1980-05-15,,Cardiology\n"
    )
    imp = _seed_mapped_import(client, storage, user_id, csv_body)
    rows = storage.list_csv_import_rows(Scope(user_id=user_id), imp.id)
    assert len(rows) == 1
    assert rows[0].status == "error"
    assert "reason" in rows[0].validation_errors
    assert imp.error_report["error"] == 1


def test_validation_flags_bad_dob(solo_client):
    client, storage, user_id = solo_client
    csv_body = (
        b"patient_first,patient_last,patient_dob,reason,specialty\n"
        b"Jane,Doe,not-a-date,Eval,Cardiology\n"
    )
    imp = _seed_mapped_import(client, storage, user_id, csv_body)
    rows = storage.list_csv_import_rows(Scope(user_id=user_id), imp.id)
    assert rows[0].status == "error"
    assert "patient_dob" in rows[0].validation_errors


def test_validation_flags_bad_npi(solo_client):
    client, storage, user_id = solo_client
    csv_body = b"patient_first,patient_last,reason,npi\nJane,Doe,Eval,abc123\n"
    # Full mapping including NPI
    client.post("/imports", files={"file": ("r.csv", csv_body, "text/csv")})
    imp = storage.list_csv_imports(Scope(user_id=user_id))[0]
    client.post(
        f"/imports/{imp.id}/map",
        data={
            "col__patient_first": "patient_first_name",
            "col__patient_last": "patient_last_name",
            "col__reason": "reason",
            "col__npi": "receiving_provider_npi",
        },
    )
    rows = storage.list_csv_import_rows(Scope(user_id=user_id), imp.id)
    assert rows[0].status == "error"
    assert "receiving_provider_npi" in rows[0].validation_errors


def test_review_page_renders(solo_client):
    client, storage, user_id = solo_client
    imp = _seed_mapped_import(client, storage, user_id)
    resp = client.get(f"/imports/{imp.id}/review")
    assert resp.status_code == 200
    assert "Jane" in resp.text
    assert "Chest pain eval" in resp.text
    assert "Commit 2 valid" in resp.text


def test_row_inline_edit_fixes_errored_row(solo_client):
    """Edit the errored cell + re-validate flips status valid."""
    client, storage, user_id = solo_client
    csv_body = (
        b"patient_first,patient_last,patient_dob,reason,specialty\n"
        b"Jane,Doe,1980-05-15,,Cardiology\n"  # blank reason → error
    )
    imp = _seed_mapped_import(client, storage, user_id, csv_body)
    rows = storage.list_csv_import_rows(Scope(user_id=user_id), imp.id)
    errored = rows[0]
    assert errored.status == "error"

    resp = client.post(
        f"/imports/{imp.id}/rows/{errored.id}/edit",
        data={
            "cell__patient_first": "Jane",
            "cell__patient_last": "Doe",
            "cell__patient_dob": "1980-05-15",
            "cell__reason": "Follow-up eval",  # fills the blank
            "cell__specialty": "Cardiology",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    updated = storage.list_csv_import_rows(Scope(user_id=user_id), imp.id)[0]
    assert updated.status == "valid"
    assert updated.validation_errors == {}
    assert updated.raw_json["reason"] == "Follow-up eval"


def test_validate_rejects_unmapped_import(solo_client):
    client, storage, user_id = solo_client
    client.post("/imports", files={"file": ("r.csv", _SAMPLE_CSV, "text/csv")})
    imp = storage.list_csv_imports(Scope(user_id=user_id))[0]
    # imp is in 'uploaded' — no mapping yet
    resp = client.post(f"/imports/{imp.id}/validate")
    assert resp.status_code == 409


# --- Review follow-ups ---


def test_authorization_status_forwarded_on_commit(solo_client):
    """Previously dropped at commit — PR #97 review."""
    client, storage, user_id = solo_client
    csv_body = (
        b"patient_first,patient_last,patient_dob,reason,specialty,auth_status\n"
        b"Jane,Doe,1980-05-15,Eval,Cardiology,obtained\n"
    )
    client.post("/imports", files={"file": ("r.csv", csv_body, "text/csv")})
    scope = Scope(user_id=user_id)
    imp = storage.list_csv_imports(scope)[0]
    client.post(
        f"/imports/{imp.id}/map",
        data={
            "col__patient_first": "patient_first_name",
            "col__patient_last": "patient_last_name",
            "col__patient_dob": "patient_dob",
            "col__reason": "reason",
            "col__specialty": "specialty_desc",
            "col__auth_status": "authorization_status",
        },
    )
    client.post(f"/imports/{imp.id}/commit")
    referrals = storage.list_referrals(scope)
    assert len(referrals) == 1
    assert referrals[0].authorization_status == "obtained"


def test_malformed_csv_returns_422(solo_client, monkeypatch):
    """csv.Error inside the iteration loop should surface as 422, not 500.

    Python's csv module is quite permissive — unterminated quotes and NUL
    bytes don't raise csv.Error in practice. To exercise the handler, we
    monkeypatch DictReader iteration to raise directly.
    """
    import csv as csv_mod

    from docstats.routes import imports as imports_mod

    class _BoomReader:
        fieldnames = ["a"]

        def __iter__(self):
            raise csv_mod.Error("unterminated quoted field")

    monkeypatch.setattr(imports_mod.csv, "DictReader", lambda buf: _BoomReader())
    client, _, _ = solo_client
    resp = client.post("/imports", files={"file": ("bad.csv", b"a\nx\n", "text/csv")})
    assert resp.status_code == 422
    assert "malformed" in resp.text.lower()


def test_future_dob_rejected_in_csv(solo_client):
    client, storage, user_id = solo_client
    csv_body = (
        b"patient_first,patient_last,patient_dob,reason,specialty\n"
        b"Jane,Doe,2099-01-01,Eval,Cardiology\n"
    )
    imp = _seed_mapped_import(client, storage, user_id, csv_body)
    rows = storage.list_csv_import_rows(Scope(user_id=user_id), imp.id)
    assert rows[0].status == "error"
    assert "future" in rows[0].validation_errors.get("patient_dob", "")


def test_future_dob_rejected_on_patient_form(tmp_path: Path):
    """routes/patients.py::_validate_dob should match the CSV behavior."""
    storage = Storage(db_path=tmp_path / "test.db")
    user_id = storage.create_user("a@example.com", "hashed")
    storage.record_phi_consent(
        user_id=user_id,
        phi_consent_version=CURRENT_PHI_CONSENT_VERSION,
        ip_address="",
        user_agent="",
    )
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_current_user] = lambda: _fake_user(user_id)
    try:
        client = TestClient(app)
        resp = client.post(
            "/patients",
            data={"first_name": "Jane", "last_name": "Doe", "date_of_birth": "2099-01-01"},
        )
        assert resp.status_code == 422
    finally:
        app.dependency_overrides.clear()


def test_mrn_exact_match_reuses_patient(solo_client):
    """Upload a CSV whose MRN matches an existing patient — reuse, don't duplicate."""
    client, storage, user_id = solo_client
    scope = Scope(user_id=user_id)
    existing = storage.create_patient(
        scope,
        first_name="Existing",
        last_name="Patient",
        mrn="MRN-12345",
        created_by_user_id=user_id,
    )
    csv_body = b"patient_first,patient_last,patient_mrn,reason\nFresh,Name,MRN-12345,Eval\n"
    client.post("/imports", files={"file": ("r.csv", csv_body, "text/csv")})
    imp = storage.list_csv_imports(scope)[0]
    client.post(
        f"/imports/{imp.id}/map",
        data={
            "col__patient_first": "patient_first_name",
            "col__patient_last": "patient_last_name",
            "col__patient_mrn": "patient_mrn",
            "col__reason": "reason",
        },
    )
    client.post(f"/imports/{imp.id}/commit")
    patients = storage.list_patients(scope)
    # No new patient — the existing one was reused by exact MRN match.
    assert len(patients) == 1
    assert patients[0].id == existing.id
    referrals = storage.list_referrals(scope)
    assert referrals[0].patient_id == existing.id


def test_list_patients_mrn_kwarg_exact_match(solo_client):
    """Direct storage-level test of the new kwarg."""
    _, storage, user_id = solo_client
    scope = Scope(user_id=user_id)
    storage.create_patient(
        scope, first_name="A", last_name="B", mrn="X", created_by_user_id=user_id
    )
    storage.create_patient(
        scope, first_name="C", last_name="D", mrn="Y", created_by_user_id=user_id
    )
    got = storage.list_patients(scope, mrn="X")
    assert len(got) == 1
    assert got[0].mrn == "X"
    assert storage.list_patients(scope, mrn="ZZ") == []


def test_content_length_cap(solo_client):
    """Oversized Content-Length header rejected before body read."""
    client, _, _ = solo_client
    # httpx/TestClient will honor an explicit Content-Length header; fabricate
    # by pretending a tiny body is huge.
    resp = client.post(
        "/imports",
        files={"file": ("r.csv", b"col\nval\n", "text/csv")},
        headers={"content-length": str(10 * 1024 * 1024)},  # 10 MB, above cap
    )
    # TestClient may or may not forward the fake header verbatim; if it does,
    # we get 422. If it doesn't, the body cap still fires on the server read.
    # Either way the server MUST reject oversized uploads.
    assert resp.status_code == 422 or (resp.status_code in (400, 413, 422))


def test_orphan_patient_compensation(solo_client, monkeypatch):
    """If referral create fails AFTER a NEW patient was created, the patient
    is soft-deleted so it doesn't orphan. Pre-existing matched patients are
    not touched."""
    client, storage, user_id = solo_client
    scope = Scope(user_id=user_id)

    # Seed a validated import with 1 valid row; the MRN is NEW so the commit
    # path takes the create-patient branch.
    csv_body = b"patient_first,patient_last,patient_mrn,reason\nFresh,Patient,NEW-MRN-001,Eval\n"
    client.post("/imports", files={"file": ("r.csv", csv_body, "text/csv")})
    imp = storage.list_csv_imports(scope)[0]
    client.post(
        f"/imports/{imp.id}/map",
        data={
            "col__patient_first": "patient_first_name",
            "col__patient_last": "patient_last_name",
            "col__patient_mrn": "patient_mrn",
            "col__reason": "reason",
        },
    )

    # Patch create_referral to always raise
    orig_create_referral = storage.create_referral

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated referral-create failure")

    monkeypatch.setattr(storage, "create_referral", _boom)
    client.post(f"/imports/{imp.id}/commit")
    monkeypatch.setattr(storage, "create_referral", orig_create_referral)

    # Patient with the new MRN was soft-deleted (not visible in active list)
    active = storage.list_patients(scope)
    assert all(p.mrn != "NEW-MRN-001" for p in active)


# --- Downloadable template (Phase 4.E) ---


def test_template_csv_download(solo_client):
    client, _, _ = solo_client
    resp = client.get("/imports/template.csv")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "attachment" in resp.headers["content-disposition"]
    text = resp.text
    # Canonical target-key headers land literally so mapping auto-matches.
    assert "patient_first_name" in text
    assert "patient_last_name" in text
    assert "reason" in text
    # Plus one sample data row
    assert "Jane" in text
    assert "Doe" in text
    assert "207RC0000X" in text


# --- Commit + summary + error report (Phase 4.D) ---


def test_commit_creates_referrals_and_patients(solo_client):
    client, storage, user_id = solo_client
    imp = _seed_mapped_import(client, storage, user_id)
    scope = Scope(user_id=user_id)
    assert imp.status == "validated"

    resp = client.post(f"/imports/{imp.id}/commit", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/imports/{imp.id}/summary"

    updated = storage.get_csv_import(scope, imp.id)
    assert updated.status == "committed"
    assert updated.error_report["committed"] == 2

    # Two patients + two referrals exist, all linked via row.referral_id
    patients = storage.list_patients(scope)
    assert len(patients) == 2
    rows = storage.list_csv_import_rows(scope, imp.id)
    assert all(r.status == "committed" for r in rows)
    assert all(r.referral_id is not None for r in rows)
    events = storage.list_audit_events(limit=10)
    assert any(e.action == "import.commit" for e in events)


def test_commit_rejects_non_validated(solo_client):
    client, storage, user_id = solo_client
    client.post("/imports", files={"file": ("r.csv", _SAMPLE_CSV, "text/csv")})
    imp = storage.list_csv_imports(Scope(user_id=user_id))[0]
    # Still 'uploaded' — no map, no validate
    resp = client.post(f"/imports/{imp.id}/commit")
    assert resp.status_code == 409


def test_commit_skips_errored_rows(solo_client):
    client, storage, user_id = solo_client
    csv_body = (
        b"patient_first,patient_last,patient_dob,reason,specialty\n"
        b"Jane,Doe,1980-05-15,Eval 1,Cardiology\n"
        b"Bob,Smith,1975-03-02,,Neurology\n"  # blank reason → errored
    )
    imp = _seed_mapped_import(client, storage, user_id, csv_body)
    client.post(f"/imports/{imp.id}/commit")

    scope = Scope(user_id=user_id)
    updated = storage.get_csv_import(scope, imp.id)
    assert updated.error_report["committed"] == 1
    assert updated.error_report["skipped_error"] == 1
    # Only one patient created; errored row didn't insert
    assert len(storage.list_patients(scope)) == 1


def test_summary_page_renders(solo_client):
    client, storage, user_id = solo_client
    imp = _seed_mapped_import(client, storage, user_id)
    client.post(f"/imports/{imp.id}/commit")
    resp = client.get(f"/imports/{imp.id}/summary")
    assert resp.status_code == 200
    assert ">2</strong> referrals created" in resp.text
    assert "status-committed" in resp.text


def test_error_report_csv(solo_client):
    client, storage, user_id = solo_client
    csv_body = (
        b"patient_first,patient_last,patient_dob,reason,specialty\n"
        b"Jane,Doe,1980-05-15,,Cardiology\n"  # blank reason → error
    )
    imp = _seed_mapped_import(client, storage, user_id, csv_body)
    resp = client.get(f"/imports/{imp.id}/error-report.csv")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    text = resp.text
    assert "_row_index" in text
    assert "_errors" in text
    assert "Jane" in text
    assert "reason: required" in text


def test_commit_second_time_is_rejected(solo_client):
    """Once an import is 'committed' you can't re-run it — terminal state."""
    client, storage, user_id = solo_client
    imp = _seed_mapped_import(client, storage, user_id)
    client.post(f"/imports/{imp.id}/commit")
    resp = client.post(f"/imports/{imp.id}/commit")
    assert resp.status_code == 409


# --- Cross-tenant isolation ---


def test_cross_user_isolation(tmp_path: Path):
    storage = Storage(db_path=tmp_path / "test.db")
    uid_a = storage.create_user("a@example.com", "hashed")
    uid_b = storage.create_user("b@example.com", "hashed")
    for uid in (uid_a, uid_b):
        storage.record_phi_consent(
            user_id=uid,
            phi_consent_version=CURRENT_PHI_CONSENT_VERSION,
            ip_address="",
            user_agent="",
        )

    # User B uploads
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_current_user] = lambda: _fake_user(uid_b)
    try:
        TestClient(app).post(
            "/imports",
            files={"file": ("private.csv", _SAMPLE_CSV, "text/csv")},
        )
    finally:
        app.dependency_overrides.clear()

    imp_b = storage.list_csv_imports(Scope(user_id=uid_b))[0]

    # User A cannot see it
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_current_user] = lambda: _fake_user(uid_a)
    try:
        client = TestClient(app)
        resp = client.get("/imports")
        assert "private.csv" not in resp.text
        # And the direct open is 404
        resp = client.get(f"/imports/{imp_b.id}/map")
        assert resp.status_code == 404
    finally:
        app.dependency_overrides.clear()
