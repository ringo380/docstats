"""Referral export routes (Phase 5.A + 5.B).

5.A shipped the Referral Request Summary PDF. 5.B expands the artifact set
with scheduling/patient-friendly summaries, attachments checklist, and a
rules-engine-driven missing-info checklist. Each artifact rides the same
route surface — ``GET /referrals/{id}/export.pdf?artifact=<name>`` —
dispatched via :data:`_ARTIFACT_RENDERERS`.

Route contract:

- PHI-consent gated (``require_phi_consent``).
- Scope-enforced via ``get_scope``; cross-tenant IDs 404.
- WeasyPrint rendering runs in the default thread executor so the CPU-bound
  HTML→PDF pipeline doesn't block uvicorn's event loop.
- Best-effort audit (``referral.export``) + referral event (``exported``).

5.C adds the preview UI with per-artifact toggles + fax cover; 5.D JSON;
5.E CSV + batch-export.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import Response

from docstats.domain.audit import record as audit_record
from docstats.domain.rules import rules_based_completeness
from docstats.exports import (
    ARTIFACT_ATTACHMENTS_CHECKLIST,
    ARTIFACT_MISSING_INFO,
    ARTIFACT_PATIENT_SUMMARY,
    ARTIFACT_REFERRAL_SUMMARY,
    ARTIFACT_SCHEDULING_SUMMARY,
    render_attachments_checklist,
    render_missing_info,
    render_patient_summary,
    render_referral_summary,
    render_scheduling_summary,
)
from docstats.phi import require_phi_consent
from docstats.routes._common import get_scope
from docstats.scope import Scope
from docstats.storage import get_storage
from docstats.storage_base import StorageBase

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/referrals", tags=["exports"])


# Per-artifact bundles describe: what storage calls to make, which renderer
# to invoke, and what filename stem to stamp on the download. The route
# handler stays dumb — it just fetches what the bundle asks for and calls
# the renderer. Adding a new artifact (fax cover in 5.C, etc.) means adding
# one more bundle.
#
# ``fetcher`` returns the kwargs dict passed to the renderer. Every fetcher
# receives the same inputs (storage, scope, referral, patient) + the base
# header args (``generated_at`` / ``generated_by_label``). Returning only
# the extra kwargs keeps each bundle readable.


def _fetch_for_summary(
    *,
    storage: StorageBase,
    scope: Scope,
    referral: Any,
    patient: Any,
) -> dict[str, Any]:
    return {
        "diagnoses": storage.list_referral_diagnoses(scope, referral.id),
        "medications": storage.list_referral_medications(scope, referral.id),
        "allergies": storage.list_referral_allergies(scope, referral.id),
        "attachments": storage.list_referral_attachments(scope, referral.id),
    }


def _fetch_for_attachments(
    *,
    storage: StorageBase,
    scope: Scope,
    referral: Any,
    patient: Any,
) -> dict[str, Any]:
    return {"attachments": storage.list_referral_attachments(scope, referral.id)}


def _fetch_for_missing_info(
    *,
    storage: StorageBase,
    scope: Scope,
    referral: Any,
    patient: Any,
) -> dict[str, Any]:
    return {"completeness": rules_based_completeness(storage, scope, referral)}


def _fetch_none(
    *,
    storage: StorageBase,
    scope: Scope,
    referral: Any,
    patient: Any,
) -> dict[str, Any]:
    return {}


_ARTIFACT_RENDERERS: dict[str, tuple[Callable[..., Any], Callable[..., bytes], str]] = {
    ARTIFACT_REFERRAL_SUMMARY: (_fetch_for_summary, render_referral_summary, "summary"),
    ARTIFACT_SCHEDULING_SUMMARY: (_fetch_none, render_scheduling_summary, "scheduling"),
    ARTIFACT_PATIENT_SUMMARY: (_fetch_none, render_patient_summary, "patient"),
    ARTIFACT_ATTACHMENTS_CHECKLIST: (
        _fetch_for_attachments,
        render_attachments_checklist,
        "attachments",
    ),
    ARTIFACT_MISSING_INFO: (_fetch_for_missing_info, render_missing_info, "missing-info"),
}


def _generated_by_label(user: dict) -> str | None:
    """Human-readable actor for the PDF footer."""
    first = (user.get("first_name") or "").strip()
    last = (user.get("last_name") or "").strip()
    if first or last:
        return f"{first} {last}".strip()
    return user.get("display_name") or user.get("email")


def _safe_pdf_filename(referral_id: int, stem: str) -> str:
    """Content-Disposition filenames must survive ``require_valid_npi``-grade
    paranoia. We control both the referral id and stem — no PHI leaks."""
    return f"referral-{referral_id}-{stem}.pdf"


@router.get("/{referral_id}/export.pdf")
async def referral_export_pdf(
    request: Request,
    referral_id: int = Path(..., ge=1),
    artifact: str = Query(ARTIFACT_REFERRAL_SUMMARY, max_length=32),
    current_user: dict = Depends(require_phi_consent),
    scope: Scope = Depends(get_scope),
    storage: StorageBase = Depends(get_storage),
) -> Response:
    bundle = _ARTIFACT_RENDERERS.get(artifact)
    if bundle is None:
        raise HTTPException(status_code=400, detail=f"Unsupported artifact '{artifact}'.")
    fetcher, renderer, stem = bundle

    referral = storage.get_referral(scope, referral_id)
    if referral is None:
        raise HTTPException(status_code=404, detail="Referral not found.")

    patient = storage.get_patient(scope, referral.patient_id)
    if patient is None:
        raise HTTPException(status_code=409, detail="Patient record unavailable.")

    extra_kwargs = fetcher(storage=storage, scope=scope, referral=referral, patient=patient)

    # TOCTOU re-check: if the referral was soft-deleted between the initial
    # read and the sub-entity / rules-engine fetches, those calls quietly
    # return empty data. Re-reading collapses the race to "deleted between
    # this line and write_pdf" — tiny and graceful.
    if storage.get_referral(scope, referral_id) is None:
        raise HTTPException(status_code=404, detail="Referral not found.")

    generated_at = datetime.now(tz=timezone.utc)
    generated_by_label = _generated_by_label(current_user)

    render_kwargs: dict[str, Any] = {
        "referral": referral,
        "patient": patient,
        "generated_at": generated_at,
        "generated_by_label": generated_by_label,
        **extra_kwargs,
    }

    # WeasyPrint is CPU-bound — offload to the default executor so the
    # uvicorn event loop stays free for other requests.
    try:
        loop = asyncio.get_running_loop()
        pdf_bytes = await loop.run_in_executor(None, lambda: renderer(**render_kwargs))
    except Exception:
        logger.exception(
            "WeasyPrint render failed for referral %s artifact %s", referral_id, artifact
        )
        raise HTTPException(status_code=500, detail="Failed to render PDF.")

    # ``audit_record`` swallows its own errors; no outer try/except needed.
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

    # Best-effort referral-event — ``record_referral_event`` can raise on
    # a transient storage blip and we don't want that to fail the response.
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

    filename = _safe_pdf_filename(referral_id, stem)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{filename}"',
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
