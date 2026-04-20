"""WeasyPrint-based PDF rendering for referral export artifacts.

This module is FastAPI-free. Callers pass in a ``Referral`` + already-fetched
related rows and get back PDF bytes. Scope gating + row fetches live in
``docstats.routes.exports``.

WeasyPrint needs system libraries (``libpango``, ``libcairo``, ``libharfbuzz``)
which are declared in ``railpack.json``. Local macOS dev typically needs
``brew install pango cairo``.
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


_TEMPLATE_DIR = Path(__file__).parents[1] / "templates" / "exports"
_STATIC_DIR = Path(__file__).parents[1] / "static"

_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=select_autoescape(["html", "xml"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


ARTIFACT_REFERRAL_SUMMARY = "summary"


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


def _build_summary_context(
    *,
    referral: "Referral",
    patient: "Patient",
    diagnoses: list["ReferralDiagnosis"],
    medications: list["ReferralMedication"],
    allergies: list["ReferralAllergy"],
    attachments: list["ReferralAttachment"],
    generated_at: datetime,
    generated_by_label: str | None,
) -> dict[str, Any]:
    return {
        "referral": referral,
        "patient": patient,
        "patient_age": _age_years(patient.date_of_birth, as_of=generated_at),
        "patient_phone": _fmt_phone(patient.phone),
        "diagnoses": diagnoses,
        "medications": medications,
        "allergies": allergies,
        "attachments": attachments,
        "pending_attachments": [a for a in attachments if a.checklist_only],
        "included_attachments": [a for a in attachments if not a.checklist_only],
        "generated_at": generated_at,
        "generated_by_label": generated_by_label,
    }


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
    """Render the Referral Request Summary artifact as a PDF.

    Returns raw PDF bytes. Raises :class:`weasyprint.WeasyPrintError` or
    an ``OSError`` if the WeasyPrint system libraries are unavailable —
    the route layer surfaces these as HTTP 500 so the failure is visible.
    """
    # Lazy import so ``docstats.exports`` stays importable in environments
    # without the system libs (e.g. lint-only CI shards). The web route path
    # hits this import before any response body is produced, so a missing
    # dep surfaces as a 500 with a clear traceback.
    from weasyprint import CSS, HTML  # type: ignore[import-untyped]

    context = _build_summary_context(
        referral=referral,
        patient=patient,
        diagnoses=list(diagnoses or []),
        medications=list(medications or []),
        allergies=list(allergies or []),
        attachments=list(attachments or []),
        generated_at=generated_at or datetime.now(tz=timezone.utc),
        generated_by_label=generated_by_label,
    )

    template = _env.get_template("referral_summary.html")
    html_str = template.render(**context)

    stylesheet_path = _STATIC_DIR / "print.css"
    stylesheets = [CSS(filename=str(stylesheet_path))] if stylesheet_path.exists() else []

    pdf_bytes = HTML(string=html_str, base_url=str(_STATIC_DIR)).write_pdf(stylesheets=stylesheets)
    if pdf_bytes is None:
        # write_pdf can return None in some configurations; tighten the type.
        raise RuntimeError("WeasyPrint returned empty output")
    return bytes(pdf_bytes)
