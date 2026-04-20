"""Export pipeline — PDF, JSON, CSV artifacts for a Referral.

Phase 5 ships incrementally:

- 5.A — Referral Request Summary PDF (shipped).
- 5.B — Scheduling Summary, Patient-Friendly Summary, Attachments Checklist,
        Missing-Info Checklist (shipped here).
- 5.C — Fax Cover + packet bundle + preview UI with per-artifact toggles.
- 5.D — FHIR-ish ServiceRequest+Patient JSON export.
- 5.E — CSV export + ``POST /referrals/batch-export``.

All artifacts are pure functions of a ``Referral`` + related rows — no
database or network I/O, no hidden side-effects. Template + CSS reads from
the package directory are the only disk accesses. The route layer in
``docstats.routes.exports`` owns scope gating, audit, and HTTP concerns.
"""

from __future__ import annotations

from docstats.exports.csv_export import CSV_FIELDNAMES, referral_to_csv_row
from docstats.exports.fhir import build_referral_bundle
from docstats.exports.pdf import (
    ARTIFACT_ATTACHMENTS_CHECKLIST,
    ARTIFACT_FAX_COVER,
    ARTIFACT_MISSING_INFO,
    ARTIFACT_PACKET,
    ARTIFACT_PATIENT_SUMMARY,
    ARTIFACT_REFERRAL_SUMMARY,
    ARTIFACT_SCHEDULING_SUMMARY,
    render_attachments_checklist,
    render_fax_cover,
    render_missing_info,
    render_packet,
    render_patient_summary,
    render_referral_summary,
    render_scheduling_summary,
)

__all__ = [
    "ARTIFACT_ATTACHMENTS_CHECKLIST",
    "ARTIFACT_FAX_COVER",
    "ARTIFACT_MISSING_INFO",
    "ARTIFACT_PACKET",
    "ARTIFACT_PATIENT_SUMMARY",
    "ARTIFACT_REFERRAL_SUMMARY",
    "ARTIFACT_SCHEDULING_SUMMARY",
    "render_attachments_checklist",
    "render_fax_cover",
    "render_missing_info",
    "render_packet",
    "render_patient_summary",
    "render_referral_summary",
    "render_scheduling_summary",
    "build_referral_bundle",
    "CSV_FIELDNAMES",
    "referral_to_csv_row",
]
