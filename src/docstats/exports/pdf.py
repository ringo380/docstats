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

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

logger = logging.getLogger(__name__)

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
ARTIFACT_FAX_COVER = "fax_cover"
ARTIFACT_PACKET = "packet"
# Phase 10.D — splices real PDF attachment bytes into a packet.  This is a
# "pseudo-artifact": it can only appear inside ``?include=`` for a packet
# render; it's not renderable on its own via ``?artifact=``.  Non-PDF
# attachments (images, DOCX) are skipped at the render layer because
# pypdf can't concatenate non-PDFs without conversion — they stay in
# the ``attachments`` checklist.
ARTIFACT_ATTACHMENT_PDFS = "attachment_pdfs"


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


# ---------- Fax Cover (5.C) ----------


def render_fax_cover(
    *,
    referral: "Referral",
    patient: "Patient",
    total_pages: int | None = None,
    generated_at: datetime | None = None,
    generated_by_label: str | None = None,
) -> bytes:
    """Single-page fax cover sheet.

    When bundled into a packet, callers pass ``total_pages`` so the cover
    shows "Pages: N" correctly. Standalone exports pass None and the
    template renders "Pages: 1" by default.
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
            "total_pages": total_pages,
            "receiving_fax": _fmt_phone(_pick_fax(referral, "receiving")),
            "sender_fax": _fmt_phone(_pick_fax(referral, "sending")),
        }
    )
    return _render_pdf("fax_cover.html", context)


def _pick_fax(referral: "Referral", side: str) -> str | None:
    """Placeholder until dedicated fax fields land on Referral.

    The current Referral model doesn't carry explicit fax numbers for
    either side — Phase 9 (outbound delivery) will add them. For now the
    template renders "—" when fax is missing; coordinators can still use
    the cover sheet as a faxable front page.
    """
    return None


# ---------- Packet bundle (5.C) ----------


async def fetch_attachment_pdfs(
    *,
    storage: Any,
    scope: Any,
    referral: "Referral",
    file_backend: Any,
) -> list[tuple[int, bytes]]:
    """Phase 10.D — pull real PDF attachment bytes for packet embedding.

    Returns ``[(attachment_id, pdf_bytes), ...]`` for every attachment on
    ``referral`` whose ``storage_ref`` is set AND whose file extension
    indicates a PDF (others are skipped — pypdf can't concatenate non-PDF
    formats without conversion, so the checklist entry remains their
    only representation in the packet).

    The storage + file_backend dependencies are passed in positionally
    rather than imported at module scope so this module stays
    framework-free (the existing contract — see ``exports.__init__``).
    Callers: the packet export route + the delivery dispatcher's
    ``render_delivery_packet``.  Failures on individual attachments are
    logged and skipped so one missing blob doesn't fail the whole packet.
    """
    out: list[tuple[int, bytes]] = []
    attachments = storage.list_referral_attachments(scope, referral.id)
    for a in attachments:
        ref = a.storage_ref
        if not ref:
            continue
        # Only PDFs survive the concat — the path suffix is derived from
        # the MIME type at upload time so this is authoritative without
        # a second MIME sniff.
        if not ref.lower().endswith(".pdf"):
            logger.debug(
                "skipping non-PDF attachment %s (storage_ref=%s) in packet embed",
                a.id,
                ref,
            )
            continue
        try:
            data = await file_backend.get_bytes(ref)
        except Exception:
            logger.warning(
                "attachment %s (%s) unavailable during packet render",
                a.id,
                ref,
            )
            continue
        out.append((a.id, data))
    return out


def render_packet(
    *,
    referral: "Referral",
    patient: "Patient",
    parts: list[bytes],
    generated_at: datetime | None = None,
    generated_by_label: str | None = None,
) -> bytes:
    """Concatenate multiple PDF byte-strings into one.

    The route layer is responsible for ordering ``parts`` — typically fax
    cover first, then summary, then attachments, then anything else. This
    function just merges with pypdf. It accepts a possibly-empty list and
    raises ``ValueError`` on empty (caller-visible 400).
    """
    if not parts:
        raise ValueError("packet requires at least one part")
    if len(parts) == 1:
        return parts[0]

    # Lazy import so exports is importable without pypdf installed.
    from io import BytesIO

    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    for part in parts:
        reader = PdfReader(BytesIO(part))
        for page in reader.pages:
            writer.add_page(page)

    out = BytesIO()
    writer.write(out)
    # generated_* args accepted so the route layer can pass them uniformly;
    # we don't embed them in the packet itself (each sub-doc has its own
    # header/footer with the stamp).
    _ = generated_at, generated_by_label, referral, patient
    return out.getvalue()
