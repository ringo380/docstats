"""Referral export routes (Phase 5.A–5.C).

Ships via a single dispatch map so new artifacts are one-tuple appends:

- 5.A — ``summary`` (Referral Request Summary)
- 5.B — ``scheduling``, ``patient``, ``attachments``, ``missing_info``
- 5.C — ``fax_cover`` + ``packet`` (concatenated bundle) + preview UI at
        ``GET /referrals/{id}/export``

Route contract:

- PHI-consent gated via ``require_phi_consent``.
- Scope-enforced via ``get_scope``; cross-tenant IDs 404.
- WeasyPrint rendering runs in the default thread executor so the CPU-bound
  HTML→PDF pipeline doesn't block uvicorn's event loop.
- Best-effort audit (``referral.export``) + referral event (``exported``).

Phase 5.D adds JSON; 5.E CSV + batch-export.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from docstats.domain.audit import record as audit_record
from docstats.domain.rules import rules_based_completeness
from docstats.exports import (
    ARTIFACT_ATTACHMENTS_CHECKLIST,
    ARTIFACT_FAX_COVER,
    ARTIFACT_MISSING_INFO,
    ARTIFACT_PACKET,
    ARTIFACT_PATIENT_SUMMARY,
    ARTIFACT_REFERRAL_SUMMARY,
    ARTIFACT_SCHEDULING_SUMMARY,
    build_referral_bundle,
    render_attachments_checklist,
    render_fax_cover,
    render_missing_info,
    render_packet,
    render_patient_summary,
    render_referral_summary,
    render_scheduling_summary,
)
from docstats.phi import require_phi_consent
from docstats.routes._common import get_scope, render
from docstats.scope import Scope
from docstats.storage import get_storage
from docstats.storage_base import StorageBase

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/referrals", tags=["exports"])


# Per-artifact bundles: fetcher + renderer + filename stem + display label.
# Adding a new artifact is one tuple append.
#
# ``fetcher`` takes (storage, scope, referral, patient) and returns the
# EXTRA kwargs passed to the renderer (beyond the common base). The base
# kwargs are: referral, patient, generated_at, generated_by_label.


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


# The ``label`` field drives the preview-page UI. Keep it user-facing.
_ARTIFACT_BUNDLES: dict[str, tuple[Callable[..., Any], Callable[..., bytes], str, str]] = {
    ARTIFACT_REFERRAL_SUMMARY: (
        _fetch_for_summary,
        render_referral_summary,
        "summary",
        "Referral Request Summary",
    ),
    ARTIFACT_SCHEDULING_SUMMARY: (
        _fetch_none,
        render_scheduling_summary,
        "scheduling",
        "Specialist Scheduling Summary",
    ),
    ARTIFACT_PATIENT_SUMMARY: (
        _fetch_none,
        render_patient_summary,
        "patient",
        "Patient-Friendly Summary",
    ),
    ARTIFACT_ATTACHMENTS_CHECKLIST: (
        _fetch_for_attachments,
        render_attachments_checklist,
        "attachments",
        "Attachments Checklist",
    ),
    ARTIFACT_MISSING_INFO: (
        _fetch_for_missing_info,
        render_missing_info,
        "missing-info",
        "Missing-Info Checklist",
    ),
    ARTIFACT_FAX_COVER: (
        _fetch_none,
        render_fax_cover,
        "fax-cover",
        "Fax Cover Sheet",
    ),
}


# Packet default ordering when ``?include=`` is omitted — fax cover first,
# then summary, then attachments. Matches coordinator workflow: what the
# receiving office sees from the top of the stack.
_DEFAULT_PACKET_INCLUDE: tuple[str, ...] = (
    ARTIFACT_FAX_COVER,
    ARTIFACT_REFERRAL_SUMMARY,
    ARTIFACT_ATTACHMENTS_CHECKLIST,
)


def _generated_by_label(user: dict) -> str | None:
    first = (user.get("first_name") or "").strip()
    last = (user.get("last_name") or "").strip()
    if first or last:
        return f"{first} {last}".strip()
    return user.get("display_name") or user.get("email")


def _safe_pdf_filename(referral_id: int, stem: str) -> str:
    return f"referral-{referral_id}-{stem}.pdf"


def _render_one(
    *,
    storage: StorageBase,
    scope: Scope,
    referral: Any,
    patient: Any,
    generated_at: datetime,
    generated_by_label: str | None,
    artifact: str,
) -> bytes:
    """Render a single artifact by name. Raises KeyError if unknown."""
    fetcher, renderer, _stem, _label = _ARTIFACT_BUNDLES[artifact]
    extra = fetcher(storage=storage, scope=scope, referral=referral, patient=patient)
    return renderer(  # type: ignore[no-any-return]
        referral=referral,
        patient=patient,
        generated_at=generated_at,
        generated_by_label=generated_by_label,
        **extra,
    )


def _parse_include(raw: str | None) -> list[str]:
    """Parse a comma-separated ``?include=a,b,c`` into a dedupe-preserving
    list of known artifact names. Unknown names raise HTTPException(400)."""
    if not raw:
        return list(_DEFAULT_PACKET_INCLUDE)
    tokens = [t.strip() for t in raw.split(",") if t.strip()]
    if not tokens:
        return list(_DEFAULT_PACKET_INCLUDE)
    # Preserve caller order, drop duplicates.
    seen: dict[str, None] = {}
    for tok in tokens:
        if tok == ARTIFACT_PACKET:
            raise HTTPException(400, detail="'packet' cannot be nested inside a packet")
        if tok not in _ARTIFACT_BUNDLES:
            raise HTTPException(400, detail=f"Unknown artifact in include: '{tok}'")
        seen[tok] = None
    return list(seen)


@router.get("/{referral_id}/export", response_class=HTMLResponse)
async def referral_export_preview(
    request: Request,
    referral_id: int = Path(..., ge=1),
    current_user: dict = Depends(require_phi_consent),
    scope: Scope = Depends(get_scope),
    storage: StorageBase = Depends(get_storage),
) -> Response:
    """Preview page with per-artifact toggles + a packet-download form."""
    referral = storage.get_referral(scope, referral_id)
    if referral is None:
        raise HTTPException(status_code=404, detail="Referral not found.")
    patient = storage.get_patient(scope, referral.patient_id)
    # ``patient=None`` here renders a partial page. Consistent with the
    # detail-page contract: the preview should load even if the patient
    # was just soft-deleted, though the actual export will 409.

    # Artifact metadata for template rendering.
    artifact_rows = [
        {
            "artifact": name,
            "label": label,
            "default": name in _DEFAULT_PACKET_INCLUDE,
            "stem": stem,
        }
        for name, (_f, _r, stem, label) in _ARTIFACT_BUNDLES.items()
    ]

    return render(
        "referral_export.html",
        {
            "request": request,
            "active_page": "referrals",
            "user": current_user,
            "referral": referral,
            "patient": patient,
            "artifact_rows": artifact_rows,
            "default_include": ",".join(_DEFAULT_PACKET_INCLUDE),
        },
    )


@router.get("/{referral_id}/export.json")
async def referral_export_json(
    request: Request,
    referral_id: int = Path(..., ge=1),
    current_user: dict = Depends(require_phi_consent),
    scope: Scope = Depends(get_scope),
    storage: StorageBase = Depends(get_storage),
) -> Response:
    """FHIR-ish JSON export for interop (Phase 5.D).

    Returns a FHIR R4-shaped Bundle (type=document) with Patient +
    ServiceRequest + related resources. Not guaranteed to pass a strict
    FHIR validator — Phase 12 (SMART-on-FHIR) hardens the mapping.
    """
    referral = storage.get_referral(scope, referral_id)
    if referral is None:
        raise HTTPException(status_code=404, detail="Referral not found.")

    patient = storage.get_patient(scope, referral.patient_id)
    if patient is None:
        raise HTTPException(status_code=409, detail="Patient record unavailable.")

    diagnoses = storage.list_referral_diagnoses(scope, referral_id)
    medications = storage.list_referral_medications(scope, referral_id)
    allergies = storage.list_referral_allergies(scope, referral_id)
    attachments = storage.list_referral_attachments(scope, referral_id)

    # TOCTOU re-check — same pattern as the PDF route.
    if storage.get_referral(scope, referral_id) is None:
        raise HTTPException(status_code=404, detail="Referral not found.")

    generated_at = datetime.now(tz=timezone.utc)
    bundle = build_referral_bundle(
        referral=referral,
        patient=patient,
        diagnoses=diagnoses,
        medications=medications,
        allergies=allergies,
        attachments=attachments,
        generated_at=generated_at,
    )

    audit_record(
        storage,
        action="referral.export",
        request=request,
        actor_user_id=current_user["id"],
        scope_user_id=scope.user_id if scope.is_solo else None,
        scope_organization_id=scope.organization_id,
        entity_type="referral",
        entity_id=str(referral_id),
        metadata={"artifact": "fhir_bundle", "format": "json", "entries": len(bundle["entry"])},
    )
    try:
        storage.record_referral_event(
            scope,
            referral_id,
            event_type="exported",
            actor_user_id=current_user["id"],
            note="fhir_bundle (json)",
        )
    except Exception:
        logger.exception("Failed to record export event for referral %s", referral_id)

    filename = f"referral-{referral_id}-bundle.json"
    return JSONResponse(
        content=bundle,
        headers={
            "Content-Disposition": f'inline; filename="{filename}"',
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/{referral_id}/export.pdf")
async def referral_export_pdf(
    request: Request,
    referral_id: int = Path(..., ge=1),
    artifact: str = Query(ARTIFACT_REFERRAL_SUMMARY, max_length=32),
    include: str | None = Query(None, max_length=256),
    current_user: dict = Depends(require_phi_consent),
    scope: Scope = Depends(get_scope),
    storage: StorageBase = Depends(get_storage),
) -> Response:
    # Validate artifact first so unknown values fail before we touch the DB.
    if artifact not in _ARTIFACT_BUNDLES and artifact != ARTIFACT_PACKET:
        raise HTTPException(status_code=400, detail=f"Unsupported artifact '{artifact}'.")

    referral = storage.get_referral(scope, referral_id)
    if referral is None:
        raise HTTPException(status_code=404, detail="Referral not found.")

    patient = storage.get_patient(scope, referral.patient_id)
    if patient is None:
        raise HTTPException(status_code=409, detail="Patient record unavailable.")

    generated_at = datetime.now(tz=timezone.utc)
    generated_by_label = _generated_by_label(current_user)
    loop = asyncio.get_running_loop()

    if artifact == ARTIFACT_PACKET:
        parts_order = _parse_include(include)
        # TOCTOU re-check before rendering the (potentially large) packet.
        if storage.get_referral(scope, referral_id) is None:
            raise HTTPException(status_code=404, detail="Referral not found.")

        try:
            parts: list[bytes] = []
            # The fax-cover total_pages hint is a "close-enough" approximation:
            # a full count would require rendering everything twice, so we
            # punt until 5.E's batch export. Renderer falls back to "1" when
            # None.
            for name in parts_order:
                part = await loop.run_in_executor(
                    None,
                    lambda n=name: _render_one(  # type: ignore[misc]
                        storage=storage,
                        scope=scope,
                        referral=referral,
                        patient=patient,
                        generated_at=generated_at,
                        generated_by_label=generated_by_label,
                        artifact=n,
                    ),
                )
                parts.append(part)
            pdf_bytes = await loop.run_in_executor(
                None,
                lambda: render_packet(
                    referral=referral,
                    patient=patient,
                    parts=parts,
                    generated_at=generated_at,
                    generated_by_label=generated_by_label,
                ),
            )
        except HTTPException:
            raise
        except ValueError as e:
            raise HTTPException(400, detail=str(e))
        except Exception:
            logger.exception("Packet render failed for referral %s", referral_id)
            raise HTTPException(500, detail="Failed to render packet.")

        audit_artifact_label = f"packet:{','.join(parts_order)}"
        stem = "packet"

    else:
        # TOCTOU re-check after any sub-entity fetches the fetcher does.
        if storage.get_referral(scope, referral_id) is None:
            raise HTTPException(status_code=404, detail="Referral not found.")

        try:
            pdf_bytes = await loop.run_in_executor(
                None,
                lambda: _render_one(
                    storage=storage,
                    scope=scope,
                    referral=referral,
                    patient=patient,
                    generated_at=generated_at,
                    generated_by_label=generated_by_label,
                    artifact=artifact,
                ),
            )
        except Exception:
            logger.exception(
                "WeasyPrint render failed for referral %s artifact %s",
                referral_id,
                artifact,
            )
            raise HTTPException(status_code=500, detail="Failed to render PDF.")

        audit_artifact_label = artifact
        stem = _ARTIFACT_BUNDLES[artifact][2]

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
            "artifact": audit_artifact_label,
            "format": "pdf",
            "bytes": len(pdf_bytes),
        },
    )

    try:
        storage.record_referral_event(
            scope,
            referral_id,
            event_type="exported",
            actor_user_id=current_user["id"],
            note=f"{audit_artifact_label} (pdf)",
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
