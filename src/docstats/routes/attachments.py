"""Attachment upload + download routes — Phase 10.A.

Feature-flagged via ``ATTACHMENT_UPLOAD_ENABLED``.  When the flag is
absent/false all routes return 404 so the UI can't even discover the
endpoint — the flag flips on per-environment once the Supabase BAA signs.

Routes
------
POST /referrals/{id}/attachments        — multipart upload (kind, label,
                                          date_of_service, file)
GET  /attachments/{id}                  — scope-gated; redirects to a
                                          15-minute signed URL.  In-memory
                                          backend returns the bytes inline
                                          (``inmemory://`` stub URL is
                                          test-only).
DELETE /attachments/{id}                — removes the DB row AND the bucket
                                          object (best-effort on bucket).

Every successful download emits an ``audit_events`` row
``action=attachment.view``.  Upload emits ``attachment.create``.

Contract with ``referral_attachments``
--------------------------------------
- ``storage_ref`` holds the opaque backend path.
- ``checklist_only`` flips to False on successful byte upload (the row
  was a placeholder before; now it's backed by real bytes).
- ``source`` stays ``user_entered`` on UI upload.  ``imported_ehr`` will
  land in Phase 12 when SMART launcher autopopulates.
- Non-PDF kinds (images, DOCX) still persist; Phase 10.D's packet
  embedding decides whether to inline or reference in the checklist.
"""

from __future__ import annotations

import logging
import os
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Path,
    Request,
    UploadFile,
)
from fastapi.responses import RedirectResponse, Response

from docstats.domain.audit import record as audit_record
from docstats.domain.referrals import ATTACHMENT_KIND_VALUES
from docstats.phi import require_phi_consent
from docstats.routes._common import get_scope
from docstats.scope import Scope
from docstats.storage import get_storage
from docstats.storage_base import StorageBase
from docstats.storage_files import (
    ALLOWED_MIME_TYPES,
    MAX_UPLOAD_BYTES,
    FileNotFoundInBackend,
    MimeSniffError,
    ScannerUnavailable,
    StorageFileBackend,
    StorageFileError,
    VirusScanner,
    build_object_path,
    get_file_backend,
    get_virus_scanner,
    sniff_mime,
    virus_scan_is_required,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["attachments"])

# Route-level cap; the request body is buffered, so we also enforce the
# cap on the Content-Length header BEFORE reading bytes (see
# ``_reject_oversized``) to avoid spooling multi-GB junk to disk.
_LABEL_MAX_LENGTH = 200


def _uploads_enabled() -> bool:
    return os.environ.get("ATTACHMENT_UPLOAD_ENABLED", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def _reject_if_disabled() -> None:
    if not _uploads_enabled():
        # 404 on purpose — the admin has explicitly not enabled the
        # feature; responding 403/501 would leak more than necessary.
        raise HTTPException(status_code=404)


def _reject_oversized(request: Request) -> None:
    raw = request.headers.get("content-length")
    if raw is None:
        return
    try:
        size = int(raw)
    except ValueError:
        return
    if size > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File exceeds 50 MB upload cap.")


@router.post("/referrals/{referral_id}/attachments")
async def upload_attachment(
    request: Request,
    referral_id: Annotated[int, Path(ge=1)],
    file: Annotated[UploadFile, File(...)],
    kind: Annotated[str, Form(..., max_length=32)] = "other",
    label: Annotated[str, Form(..., max_length=_LABEL_MAX_LENGTH)] = "",
    date_of_service: Annotated[str | None, Form(max_length=10)] = None,
    current_user: dict = Depends(require_phi_consent),
    scope: Scope = Depends(get_scope),
    storage: StorageBase = Depends(get_storage),
    file_backend: StorageFileBackend = Depends(get_file_backend),
    virus_scanner: VirusScanner | None = Depends(get_virus_scanner),
) -> Response:
    _reject_if_disabled()
    _reject_oversized(request)

    if kind not in ATTACHMENT_KIND_VALUES:
        raise HTTPException(status_code=422, detail=f"Unknown attachment kind {kind!r}.")

    label_clean = (label or "").strip()
    if not label_clean:
        raise HTTPException(status_code=422, detail="Label is required.")

    # Scope-gate via the parent referral.
    referral = storage.get_referral(scope, referral_id)
    if referral is None:
        raise HTTPException(status_code=404, detail="Referral not found.")

    # Read bytes with a hard cap.  Starlette will already have spooled the
    # full body by the time we're here (the Content-Length check above
    # short-circuits the obvious abuse); the second cap defends against
    # chunked uploads that omit Content-Length.
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File exceeds 50 MB upload cap.")
    if not data:
        raise HTTPException(status_code=422, detail="File is empty.")

    try:
        mime = sniff_mime(data)
    except MimeSniffError as exc:
        raise HTTPException(status_code=415, detail=str(exc))
    if mime not in ALLOWED_MIME_TYPES:  # paranoia — sniff_mime is the gate
        raise HTTPException(status_code=415, detail=f"Unsupported MIME {mime!r}.")

    # Phase 10.B — scan bytes BEFORE they leave our process.  Two failure
    # modes the route must distinguish:
    #   - definitive infected verdict → 422 with audit (attachment.scan_rejected)
    #   - scanner unavailable → policy-gated: 502 if VIRUS_SCAN_REQUIRED=1,
    #     else log-and-proceed (dev mode)
    scanner_name = "none"
    if virus_scanner is not None:
        try:
            verdict = await virus_scanner.scan(data, filename=file.filename)
        except ScannerUnavailable as exc:
            if virus_scan_is_required():
                logger.warning(
                    "Virus scan unavailable (%s) with VIRUS_SCAN_REQUIRED=1 — rejecting upload",
                    exc,
                )
                audit_record(
                    storage,
                    action="attachment.scan_unavailable",
                    request=request,
                    actor_user_id=current_user["id"],
                    scope_user_id=scope.user_id if scope.is_solo else None,
                    scope_organization_id=scope.organization_id,
                    entity_type="referral",
                    entity_id=str(referral_id),
                    metadata={"reason": str(exc)[:200]},
                )
                raise HTTPException(
                    status_code=502,
                    detail="Virus scanner unavailable; please retry.",
                )
            logger.warning("Virus scan unavailable in permissive mode: %s", exc)
        else:
            scanner_name = verdict.scanner_name
            if verdict.infected:
                audit_record(
                    storage,
                    action="attachment.scan_rejected",
                    request=request,
                    actor_user_id=current_user["id"],
                    scope_user_id=scope.user_id if scope.is_solo else None,
                    scope_organization_id=scope.organization_id,
                    entity_type="referral",
                    entity_id=str(referral_id),
                    metadata={
                        "scanner": verdict.scanner_name,
                        "threats": verdict.threat_names,
                        "mime_type": mime,
                        "size_bytes": len(data),
                    },
                )
                # Name the threats in the response only when there are any;
                # Cloudmersive doesn't always populate the list on a positive
                # hit and we don't want to leak a bare "Infected." message.
                threats = ", ".join(verdict.threat_names) if verdict.threat_names else "unknown"
                raise HTTPException(
                    status_code=422,
                    detail=f"File failed virus scan ({threats}).",
                )
    elif virus_scan_is_required():
        # VIRUS_SCAN_REQUIRED=1 but the factory returned None (backend=none).
        # This is a misconfiguration — fail closed loudly.
        logger.error("VIRUS_SCAN_REQUIRED=1 but no scanner is configured — rejecting upload")
        raise HTTPException(
            status_code=502,
            detail="Virus scanner not configured.",
        )

    # Insert the DB row first (still checklist_only = False) so we know the
    # attachment id before building the object path.  If the bucket upload
    # fails we roll back the row via ``delete_referral_attachment`` so we
    # never leave orphan rows pointing at non-existent blobs.
    attachment = storage.add_referral_attachment(
        scope,
        referral_id,
        kind=kind,
        label=label_clean,
        date_of_service=date_of_service or None,
        checklist_only=False,
        source="user_entered",
    )
    if attachment is None:
        # get_referral check already passed — race (referral soft-deleted
        # between the guard and the insert).  Treat as 404.
        raise HTTPException(status_code=404, detail="Referral not found.")

    object_path = build_object_path(
        scope=scope,
        referral_id=referral_id,
        attachment_id=attachment.id,
        mime_type=mime,
    )

    try:
        file_ref = await file_backend.put(path=object_path, data=data, mime_type=mime)
    except StorageFileError:
        # Roll back the placeholder row so we don't leave a phantom.
        storage.delete_referral_attachment(scope, referral_id, attachment.id)
        logger.exception("Attachment upload failed for referral %s", referral_id)
        raise HTTPException(status_code=502, detail="Upload failed; please retry.")

    storage.update_referral_attachment(
        scope,
        referral_id,
        attachment.id,
        storage_ref=file_ref.storage_ref,
    )

    audit_record(
        storage,
        action="attachment.create",
        request=request,
        actor_user_id=current_user["id"],
        scope_user_id=scope.user_id if scope.is_solo else None,
        scope_organization_id=scope.organization_id,
        entity_type="attachment",
        entity_id=str(attachment.id),
        metadata={
            "referral_id": referral_id,
            "kind": kind,
            "mime_type": mime,
            "size_bytes": file_ref.size_bytes,
            "scanner": scanner_name,
        },
    )

    return RedirectResponse(f"/referrals/{referral_id}", status_code=303)


@router.get("/attachments/{attachment_id}")
async def download_attachment(
    request: Request,
    attachment_id: Annotated[int, Path(ge=1)],
    current_user: dict = Depends(require_phi_consent),
    scope: Scope = Depends(get_scope),
    storage: StorageBase = Depends(get_storage),
    file_backend: StorageFileBackend = Depends(get_file_backend),
) -> Response:
    _reject_if_disabled()

    attachment = storage.get_referral_attachment(scope, attachment_id)
    if attachment is None or not attachment.storage_ref:
        raise HTTPException(status_code=404, detail="Attachment not found.")

    try:
        url = await file_backend.signed_url(attachment.storage_ref)
    except FileNotFoundInBackend:
        raise HTTPException(status_code=404, detail="Attachment bytes missing.")
    except StorageFileError:
        logger.exception("Signed URL failed for attachment %s", attachment_id)
        raise HTTPException(status_code=502, detail="Storage unavailable; please retry.")

    audit_record(
        storage,
        action="attachment.view",
        request=request,
        actor_user_id=current_user["id"],
        scope_user_id=scope.user_id if scope.is_solo else None,
        scope_organization_id=scope.organization_id,
        entity_type="attachment",
        entity_id=str(attachment.id),
        metadata={"referral_id": attachment.referral_id},
    )

    return RedirectResponse(url, status_code=302)


@router.delete("/attachments/{attachment_id}")
async def delete_attachment(
    request: Request,
    attachment_id: Annotated[int, Path(ge=1)],
    current_user: dict = Depends(require_phi_consent),
    scope: Scope = Depends(get_scope),
    storage: StorageBase = Depends(get_storage),
    file_backend: StorageFileBackend = Depends(get_file_backend),
) -> Response:
    _reject_if_disabled()

    attachment = storage.get_referral_attachment(scope, attachment_id)
    if attachment is None:
        raise HTTPException(status_code=404, detail="Attachment not found.")

    # DB first so a bucket outage doesn't leave the user staring at a row
    # they can't remove.  Orphan bucket bytes are swept by 10.C retention.
    removed = storage.delete_referral_attachment(scope, attachment.referral_id, attachment.id)
    if not removed:
        raise HTTPException(status_code=404, detail="Attachment not found.")

    if attachment.storage_ref:
        try:
            await file_backend.delete(attachment.storage_ref)
        except Exception:
            # Matches SupabaseFileBackend.delete's log-and-swallow
            # contract — the row is already gone.
            logger.exception(
                "Attachment delete: bucket cleanup failed for %s", attachment.storage_ref
            )

    audit_record(
        storage,
        action="attachment.delete",
        request=request,
        actor_user_id=current_user["id"],
        scope_user_id=scope.user_id if scope.is_solo else None,
        scope_organization_id=scope.organization_id,
        entity_type="attachment",
        entity_id=str(attachment.id),
        metadata={"referral_id": attachment.referral_id},
    )

    return Response(status_code=204)
