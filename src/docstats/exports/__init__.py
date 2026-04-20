"""Export pipeline — PDF, JSON, CSV artifacts for a Referral.

Phase 5 ships incrementally:

- 5.A — Referral Request Summary PDF (this phase).
- 5.B — Scheduling Summary, Patient-Friendly Summary, Attachments Checklist,
        Missing-Info Checklist.
- 5.C — Fax Cover + packet bundle + preview UI with per-artifact toggles.
- 5.D — FHIR-ish ServiceRequest+Patient JSON export.
- 5.E — CSV export + ``POST /referrals/batch-export``.

All artifacts are pure functions of a ``Referral`` + related rows — no
database I/O, no network I/O, no hidden side-effects. Template + CSS reads
from the package directory are the only disk accesses. The route layer in
``docstats.routes.exports`` owns scope gating, audit, and HTTP concerns.
"""

from __future__ import annotations

from docstats.exports.pdf import ARTIFACT_REFERRAL_SUMMARY, render_referral_summary

__all__ = ["ARTIFACT_REFERRAL_SUMMARY", "render_referral_summary"]
