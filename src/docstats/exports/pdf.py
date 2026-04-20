"""WeasyPrint-based PDF rendering for referral export artifacts.

This module is FastAPI-free. Callers pass in a ``Referral`` + already-fetched
related rows and get back PDF bytes. Scope gating + row fetches live in
``docstats.routes.exports``.

WeasyPrint needs system libraries (``libpango``, ``libcairo``, ``libharfbuzz``)
which are declared in ``railpack.json``. Local macOS dev typically needs
``brew install pango cairo``.

Phase 5.A shipped the Referral Request Summary. Phase 5.B adds four more:
scheduling summary (phone/fax-first for specialist front desks),
patient-friendly summary (plain language for the patient), attachments
checklist, and missing-info checklist (rules-engine driven).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

if TYPE_CHECKING:
    from docstats.domain.patients import Patient
    from docstats.domain.referrals import (
        Referral,
        ReferralAllergy,
        ReferralAttachment,
        ReferralDiagnosis,
        ReferralMedication,
    )
    from docstats.domain.rules import CompletenessReportV2


_TEMPLATE_DIR = Path(__file__).parents[1] / "templates" / "exports"
_STATIC_DIR = Path(__file__).parents[1] / "static"

_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=select_autoescape(["html", "xml"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


# Artifact identifiers — used as the ``?artifact=`` query value + the filename
# stem in ``Content-Disposition``. Keep these short, kebab-less, and stable.
ARTIFACT_REFERRAL_SUMMARY = "summary"
ARTIFACT_SCHEDULING_SUMMARY = "scheduling"
ARTIFACT_PATIENT_SUMMARY = "patient"
ARTIFACT_ATTACHMENTS_CHECKLIST = "attachments"
ARTIFACT_MISSING_INFO = "missing_info"


def _fmt_phone(raw: str | None) -> str | None:
    """Format a 10-digit phone as (XXX) XXX-XXXX; pass through anything else."""
    if not raw:
        return None
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) == 10:
        return f"({digits[0:3]}) {digits[3:6]}-{digits[6:10]}"
    return raw


def _age_years(dob: str | None, as_of: datetime | None = None) -> int | None:
    """Return integer age in years for an ISO ``YYYY-MM-DD`` DOB, or None."""
    if not dob:
        return None
    try:
        birth = datetime.strptime(dob, "%Y-%m-%d").date()
    except ValueError:
        return None
    today = (as_of or datetime.now(tz=timezone.utc)).date()
    years = today.year - birth.year
    if (today.month, today.day) < (birth.month, birth.day):
        years -= 1
    return max(years, 0)


def _render_pdf(template_name: str, context: dict[str, Any]) -> bytes:
    """Shared WeasyPrint render path: Jinja -> HTML -> PDF bytes.

    Lazy-imports ``weasyprint`` so the package stays importable without the
    system libs (e.g. lint-only CI shards).
    """
    from weasyprint import CSS, HTML  # type: ignore[import-untyped]

    template = _env.get_template(template_name)
    html_str = template.render(**context)

    stylesheet_path = _STATIC_DIR / "print.css"
    stylesheets = [CSS(filename=str(stylesheet_path))] if stylesheet_path.exists() else []

    pdf_bytes = HTML(string=html_str, base_url=str(_STATIC_DIR)).write_pdf(stylesheets=stylesheets)
    if pdf_bytes is None:
        raise RuntimeError("WeasyPrint returned empty output")
    return bytes(pdf_bytes)


def _base_context(
    *,
    referral: "Referral",
    patient: "Patient",
    generated_at: datetime,
    generated_by_label: str | None,
) -> dict[str, Any]:
    """Context keys every artifact template expects (header/footer/meta)."""
    return {
        "referral": referral,
        "patient": patient,
        "patient_age": _age_years(patient.date_of_birth, as_of=generated_at),
        "patient_phone": _fmt_phone(patient.phone),
        "generated_at": generated_at,
        "generated_by_label": generated_by_label,
    }


# ---------- Referral Request Summary (5.A) ----------


def render_referral_summary(
    *,
    referral: "Referral",
    patient: "Patient",
    diagnoses: list["ReferralDiagnosis"] | None = None,
    medications: list["ReferralMedication"] | None = None,
    allergies: list["ReferralAllergy"] | None = None,
    attachments: list["ReferralAttachment"] | None = None,
    generated_at: datetime | None = None,
    generated_by_label: str | None = None,
) -> bytes:
    """Clinical summary artifact — the 5.A anchor artifact."""
    attachments = list(attachments or [])
    now = generated_at or datetime.now(tz=timezone.utc)
    context = _base_context(
        referral=referral,
        patient=patient,
        generated_at=now,
        generated_by_label=generated_by_label,
    )
    context.update(
        {
            "diagnoses": list(diagnoses or []),
            "medications": list(medications or []),
            "allergies": list(allergies or []),
            "attachments": attachments,
            "pending_attachments": [a for a in attachments if a.checklist_only],
            "included_attachments": [a for a in attachments if not a.checklist_only],
        }
    )
    return _render_pdf("referral_summary.html", context)


# ---------- Scheduling Summary (5.B) ----------


def render_scheduling_summary(
    *,
    referral: "Referral",
    patient: "Patient",
    generated_at: datetime | None = None,
    generated_by_label: str | None = None,
) -> bytes:
    """Receiving-side front-desk artifact.

    Leads with phone/fax/authorization so a scheduler can act in seconds;
    clinical detail is intentionally minimal (reason + urgency only). If
    they want the full context, they pull the Referral Request Summary.
    """
    now = generated_at or datetime.now(tz=timezone.utc)
    context = _base_context(
        referral=referral,
        patient=patient,
        generated_at=now,
        generated_by_label=generated_by_label,
    )
    return _render_pdf("scheduling_summary.html", context)


# ---------- Patient-Friendly Summary (5.B) ----------


def render_patient_summary(
    *,
    referral: "Referral",
    patient: "Patient",
    generated_at: datetime | None = None,
    generated_by_label: str | None = None,
) -> bytes:
    """Plain-language summary the coordinator can hand or email to the patient.

    Larger type, no acronyms, no ICD codes. Focus on: who you're seeing,
    why, what to bring, and how to reach them.
    """
    now = generated_at or datetime.now(tz=timezone.utc)
    context = _base_context(
        referral=referral,
        patient=patient,
        generated_at=now,
        generated_by_label=generated_by_label,
    )
    return _render_pdf("patient_summary.html", context)


# ---------- Attachments Checklist (5.B) ----------


def render_attachments_checklist(
    *,
    referral: "Referral",
    patient: "Patient",
    attachments: list["ReferralAttachment"] | None = None,
    generated_at: datetime | None = None,
    generated_by_label: str | None = None,
) -> bytes:
    """Single-page checklist — what's attached, what's pending.

    Designed to tape to a fax cover. Pending items (checklist-only rows)
    appear with empty checkboxes so the scheduler can verify receipt.
    """
    attachments = list(attachments or [])
    now = generated_at or datetime.now(tz=timezone.utc)
    context = _base_context(
        referral=referral,
        patient=patient,
        generated_at=now,
        generated_by_label=generated_by_label,
    )
    context.update(
        {
            "attachments": attachments,
            "pending_attachments": [a for a in attachments if a.checklist_only],
            "included_attachments": [a for a in attachments if not a.checklist_only],
        }
    )
    return _render_pdf("attachments_checklist.html", context)


# ---------- Missing-Info Checklist (5.B) ----------


def render_missing_info(
    *,
    referral: "Referral",
    patient: "Patient",
    completeness: "CompletenessReportV2",
    generated_at: datetime | None = None,
    generated_by_label: str | None = None,
) -> bytes:
    """Coordinator-facing gap report — what's required vs recommended vs filled.

    Uses :class:`CompletenessReportV2` from the Phase 3 rules engine. The
    route layer builds the report via ``rules_based_completeness`` and passes
    it in so this module stays FastAPI-/storage-free.
    """
    now = generated_at or datetime.now(tz=timezone.utc)
    context = _base_context(
        referral=referral,
        patient=patient,
        generated_at=now,
        generated_by_label=generated_by_label,
    )
    context.update(
        {
            "completeness": completeness,
            "missing_required": completeness.missing_required,
            "missing_recommended": completeness.missing_recommended,
            "satisfied_items": [i for i in completeness.items if i.satisfied],
        }
    )
    return _render_pdf("missing_info.html", context)
