"""Tests for the synchronous clinician identity verifier (signup gate)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from docstats.domain.identity import ClinicianVerification, verify_clinician


def _basic(**kwargs) -> dict:
    """Minimal NPPES BasicIndividual dict (the shape that NPIResult.basic holds)."""
    base = {
        "first_name": "Steven",
        "last_name": "Dennis",
        "credential": "MD",
        "status": "A",
        "deactivation_date": None,
        "reactivation_date": None,
    }
    base.update(kwargs)
    return base


def _fake_npi_result(
    *,
    enumeration_type: str = "NPI-1",
    basic: dict | None = None,
    addresses: list[dict] | None = None,
    primary_taxonomy_code: str | None = "207RC0000X",
):
    """Construct a SimpleNamespace shaped like NPIResult for the verifier.

    Avoids importing the real Pydantic model so this test file stays
    importable in the lint-only CI shard without httpx/fastapi.
    """
    parsed = SimpleNamespace(**(_basic() if basic is None else basic))
    addr_objs = [SimpleNamespace(state=a.get("state", "")) for a in (addresses or [])]
    return SimpleNamespace(
        enumeration_type=enumeration_type,
        is_individual=enumeration_type == "NPI-1",
        addresses=addr_objs,
        primary_taxonomy=(
            SimpleNamespace(code=primary_taxonomy_code) if primary_taxonomy_code else None
        ),
        parsed_basic=lambda: parsed,
        model_dump=lambda mode="json": {
            "enumeration_type": enumeration_type,
            "basic": dict(parsed.__dict__),
        },
    )


def _fake_nppes(result):
    client = MagicMock()
    client.lookup = MagicMock(return_value=result)
    return client


def _fake_oig(hit: dict | None = None):
    client = MagicMock()
    client.check_exclusion = MagicMock(return_value=hit)
    return client


# Known-Luhn-valid NPI for happy-path tests.
GOOD_NPI = "1234567893"


# ─── Format / Luhn ─────────────────────────────────────────────


def test_rejects_non_digit_npi():
    v = verify_clinician(
        npi="abc",
        first_name="Steven",
        last_name="Dennis",
        state_license_state="TX",
        nppes=_fake_nppes(_fake_npi_result()),
        oig=_fake_oig(),
    )
    assert v.verdict == "rejected"
    assert v.reasons == ["npi_format_invalid"]


def test_rejects_bad_luhn():
    v = verify_clinician(
        npi="1234567890",  # 10 digits but bad Luhn
        first_name="Steven",
        last_name="Dennis",
        state_license_state="TX",
        nppes=_fake_nppes(_fake_npi_result()),
        oig=_fake_oig(),
    )
    assert v.verdict == "rejected"
    assert "npi_format_invalid" in v.reasons


# ─── OIG LEIE ─────────────────────────────────────────────────


def test_oig_excluded_npi_rejected_with_only_oig_reason():
    v = verify_clinician(
        npi=GOOD_NPI,
        first_name="Steven",
        last_name="Dennis",
        state_license_state="TX",
        nppes=_fake_nppes(_fake_npi_result()),
        oig=_fake_oig(hit={"NPI": GOOD_NPI, "EXCLTYPE": "1128a1"}),
    )
    assert v.verdict == "rejected"
    # Sole reason — privacy: route shows generic "contact support".
    assert v.reasons == ["oig_excluded"]


def test_oig_outage_records_soft_reason_but_continues():
    """An OIG client that raises should not block legit signups."""
    oig = MagicMock()
    oig.check_exclusion = MagicMock(side_effect=RuntimeError("LEIE CSV download failed"))
    v = verify_clinician(
        npi=GOOD_NPI,
        first_name="Steven",
        last_name="Dennis",
        state_license_state="TX",
        nppes=_fake_nppes(_fake_npi_result(addresses=[{"state": "TX"}])),
        oig=oig,
    )
    assert v.verdict == "verified"
    assert "oig_unavailable" in v.reasons


def test_oig_none_records_soft_reason():
    v = verify_clinician(
        npi=GOOD_NPI,
        first_name="Steven",
        last_name="Dennis",
        state_license_state="TX",
        nppes=_fake_nppes(_fake_npi_result(addresses=[{"state": "TX"}])),
        oig=None,
    )
    assert v.verdict == "verified"
    assert "oig_unavailable" in v.reasons


# ─── NPPES ─────────────────────────────────────────────────────


def test_npi_not_found_rejected():
    v = verify_clinician(
        npi=GOOD_NPI,
        first_name="Steven",
        last_name="Dennis",
        state_license_state="TX",
        nppes=_fake_nppes(None),
        oig=_fake_oig(),
    )
    assert v.verdict == "rejected"
    assert "npi_not_found" in v.reasons


def test_nppes_outage_pending_review():
    nppes = MagicMock()
    nppes.lookup = MagicMock(side_effect=RuntimeError("NPPES 502"))
    v = verify_clinician(
        npi=GOOD_NPI,
        first_name="Steven",
        last_name="Dennis",
        state_license_state="TX",
        nppes=nppes,
        oig=_fake_oig(),
    )
    assert v.verdict == "pending_review"
    assert "nppes_unavailable" in v.reasons


def test_deactivated_npi_rejected():
    v = verify_clinician(
        npi=GOOD_NPI,
        first_name="Steven",
        last_name="Dennis",
        state_license_state="TX",
        nppes=_fake_nppes(
            _fake_npi_result(basic=_basic(status="D", deactivation_date="2024-01-01"))
        ),
        oig=_fake_oig(),
    )
    assert v.verdict == "rejected"
    assert "npi_deactivated" in v.reasons


def test_org_npi_pending_review():
    v = verify_clinician(
        npi=GOOD_NPI,
        first_name="Bay",
        last_name="Cardiology",
        state_license_state="CA",
        nppes=_fake_nppes(
            _fake_npi_result(
                enumeration_type="NPI-2",
                basic={"status": "A", "first_name": "", "last_name": ""},
                addresses=[{"state": "CA"}],
            )
        ),
        oig=_fake_oig(),
    )
    assert v.verdict == "pending_review"
    assert "org_npi_not_individual" in v.reasons


# ─── Name + state ──────────────────────────────────────────────


def test_name_mismatch_pending_review():
    v = verify_clinician(
        npi=GOOD_NPI,
        first_name="Jane",
        last_name="Doe",
        state_license_state="TX",
        nppes=_fake_nppes(_fake_npi_result(addresses=[{"state": "TX"}])),
        oig=_fake_oig(),
    )
    assert v.verdict == "pending_review"
    assert "name_mismatch" in v.reasons


def test_state_no_overlap_pending_review():
    v = verify_clinician(
        npi=GOOD_NPI,
        first_name="Steven",
        last_name="Dennis",
        state_license_state="NY",
        nppes=_fake_nppes(_fake_npi_result(addresses=[{"state": "TX"}, {"state": "CA"}])),
        oig=_fake_oig(),
    )
    assert v.verdict == "pending_review"
    assert "state_no_overlap" in v.reasons


def test_state_no_overlap_skipped_when_no_state_provided():
    v = verify_clinician(
        npi=GOOD_NPI,
        first_name="Steven",
        last_name="Dennis",
        state_license_state=None,
        nppes=_fake_nppes(_fake_npi_result(addresses=[{"state": "TX"}])),
        oig=_fake_oig(),
    )
    assert v.verdict == "verified"
    assert "state_no_overlap" not in v.reasons


def test_happy_path_verified():
    v = verify_clinician(
        npi=GOOD_NPI,
        first_name="Steven",
        last_name="Dennis",
        state_license_state="TX",
        nppes=_fake_nppes(_fake_npi_result(addresses=[{"state": "TX"}])),
        oig=_fake_oig(),
    )
    assert v.verdict == "verified"
    assert v.primary_taxonomy == "207RC0000X"
    assert v.nppes_snapshot is not None
    assert v.method == "nppes_auto"


# ─── ClinicianVerification dataclass ───────────────────────────


def test_dataclass_immutability():
    v = ClinicianVerification(verdict="verified")
    with pytest.raises(Exception):
        v.verdict = "rejected"  # type: ignore[misc]
