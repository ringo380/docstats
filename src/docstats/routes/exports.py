"""Referral export routes (Phase 5.A).

Ships a single artifact for now — the Referral Request Summary PDF. 5.B adds
the remaining clinical artifacts; 5.C adds the preview UI with per-artifact
toggles + fax cover; 5.D JSON; 5.E CSV + batch-export.

Route contract:

- ``GET /referrals/{id}/export.pdf?artifact=summary`` — streams a PDF of the
  Referral Request Summary. PHI-consent gated (first callers alongside the
  Phase 2 patients/referrals routes). Scope-enforced via ``get_scope``;
  cross-tenant IDs 404.

Audit emits ``referral.export`` with the artifact name + byte count so the
trail tells us what left the system and how large. WeasyPrint rendering runs
in a thread executor to keep the event loop responsive — matches the
``NPPESClient`` sync-in-async pattern already used across the app.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import Response

from docstats.domain.audit import record as audit_record
from docstats.exports import ARTIFACT_REFERRAL_SUMMARY, render_referral_summary
from docstats.phi import require_phi_consent
from docstats.routes._common import get_scope
from docstats.scope import Scope
from docstats.storage import get_storage
from docstats.storage_base import StorageBase

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/referrals", tags=["exports"])

# Artifacts wired in Phase 5.A. 5.B/5.C grow this set.
_SUPPORTED_ARTIFACTS = frozenset({ARTIFACT_REFERRAL_SUMMARY})


def _generated_by_label(user: dict) -> str | None:
    """Human-readable actor for the PDF footer."""
    first = (user.get("first_name") or "").strip()
    last = (user.get("last_name") or "").strip()
    if first or last:
        return f"{first} {last}".strip()
    return user.get("display_name") or user.get("email")


def _safe_pdf_filename(referral_id: int) -> str:
    """Content-Disposition filenames must survive ``require_valid_npi``-grade
    paranoia. We control the stem entirely — no PHI in the filename."""
    return f"referral-{referral_id}-summary.pdf"


@router.get("/{referral_id}/export.pdf")
async def referral_export_pdf(
    request: Request,
    referral_id: int = Path(..., ge=1),
    artifact: str = Query(ARTIFACT_REFERRAL_SUMMARY),
    current_user: dict = Depends(require_phi_consent),
    scope: Scope = Depends(get_scope),
    storage: StorageBase = Depends(get_storage),
) -> Response:
    if artifact not in _SUPPORTED_ARTIFACTS:
        raise HTTPException(status_code=400, detail=f"Unsupported artifact '{artifact}'.")

    referral = storage.get_referral(scope, referral_id)
    if referral is None:
        raise HTTPException(status_code=404, detail="Referral not found.")

    patient = storage.get_patient(scope, referral.patient_id)
    if patient is None:
        # Patient was soft-deleted between scope check and export. Referral
        # FK is RESTRICT in theory but defensive: we never want to render
        # a summary against a missing patient.
        raise HTTPException(status_code=409, detail="Patient record unavailable.")

    diagnoses = storage.list_referral_diagnoses(scope, referral_id)
    medications = storage.list_referral_medications(scope, referral_id)
    allergies = storage.list_referral_allergies(scope, referral_id)
    attachments = storage.list_referral_attachments(scope, referral_id)

    generated_at = datetime.now(tz=timezone.utc)
    generated_by_label = _generated_by_label(current_user)

    # WeasyPrint is CPU-bound (HTML parse + layout + PDF writer). Offloading
    # to the default executor keeps uvicorn's loop free for other requests
    # while the render churns. Same pattern as NPPES sync wrappers.
    try:
        loop = asyncio.get_running_loop()
        pdf_bytes = await loop.run_in_executor(
            None,
            lambda: render_referral_summary(
                referral=referral,
                patient=patient,
                diagnoses=diagnoses,
                medications=medications,
                allergies=allergies,
                attachments=attachments,
                generated_at=generated_at,
                generated_by_label=generated_by_label,
            ),
        )
    except Exception:
        logger.exception("WeasyPrint render failed for referral %s", referral_id)
        raise HTTPException(status_code=500, detail="Failed to render PDF.")

    # Best-effort audit — a log-write blip must not poison the response.
    try:
        audit_record(
            storage,
            action="referral.export",
            request=request,
            actor_user_id=current_user["id"],
            scope_user_id=scope.user_id if scope.is_solo else None,
            scope_organization_id=scope.organization_id,
            entity_type="referral",
            entity_id=str(referral_id),
            metadata={
                "artifact": artifact,
                "format": "pdf",
                "bytes": len(pdf_bytes),
            },
        )
    except Exception:
        logger.exception("Failed to audit export of referral %s", referral_id)

    # Best-effort referral-event — same rationale.
    try:
        storage.record_referral_event(
            scope,
            referral_id,
            event_type="exported",
            actor_user_id=current_user["id"],
            note=f"{artifact} (pdf)",
        )
    except Exception:
        logger.exception("Failed to record export event for referral %s", referral_id)

    filename = _safe_pdf_filename(referral_id)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{filename}"',
            "Cache-Control": "private, max-age=0, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
