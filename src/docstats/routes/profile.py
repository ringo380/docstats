"""Profile page and PCP management routes."""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import date, datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from docstats.auth import require_user
from docstats.client import NPPESClient, NPPESError
from docstats.domain import audit
from docstats.domain.orgs import has_role_at_least
from docstats.domain.staff_access import DEFAULT_TTL_SECONDS, TTL_OPTIONS
from docstats.phi import require_phi_consent
from docstats.routes._common import MAPBOX_TOKEN, US_STATES, get_client, render, saved_count
from docstats.routes.ehr import _ehr_vendor_ui_list
from docstats.scope import Scope
from docstats.storage import get_storage
from docstats.storage_base import StorageBase
from docstats.storage_files import (
    MimeSniffError,
    StorageFileBackend,
    StorageFileError,
    get_file_backend,
    sniff_mime,
)
from docstats.validators import require_valid_npi

logger = logging.getLogger(__name__)

router = APIRouter(tags=["profile"])

_CONFIRM_PHRASE = "DELETE MY ACCOUNT"

# Signature image upload caps. Tighter than the attachment caps —
# signatures inline into every rendered letter, so they need to be
# small enough that page weight doesn't balloon.
_SIGNATURE_MAX_BYTES = 200 * 1024  # 200 KB
_SIGNATURE_ALLOWED_MIMES = frozenset({"image/png", "image/jpeg"})
_SIGNATURE_MIME_TO_SUFFIX = {"image/png": "png", "image/jpeg": "jpg"}
# Allow-list of state codes accepted on the signature form.
_US_STATE_CODES = frozenset(code for code, _ in US_STATES)
# Conservative validators applied at the route boundary; the DB also
# enforces these via NOT VALID CHECK constraints (migration 026).
_NPI_RE = re.compile(r"^[0-9]{10}$")
_LICENSE_RE = re.compile(r"^[A-Za-z0-9\-]{1,40}$")
_CREDENTIALS_MAX_LENGTH = 80
_LICENSE_NUM_MAX_LENGTH = 40


async def _signature_image_url(file_backend: StorageFileBackend, ref: str | None) -> str | None:
    """Mint a 15-min signed URL so the profile page can preview the signature
    image. Returns None when no image is set or the backend can't serve it."""
    if not ref:
        return None
    try:
        return await file_backend.signed_url(ref)
    except Exception:
        logger.exception("Failed to mint signature image URL for profile preview")
        return None


@router.get("/profile", response_class=HTMLResponse)
async def profile(
    request: Request,
    ehr_error: str | None = None,
    signature_saved: bool = False,
    signature_error: str | None = None,
    current_user: dict = Depends(require_user),
    storage: StorageBase = Depends(get_storage),
    client: NPPESClient = Depends(get_client),
    file_backend: StorageFileBackend = Depends(get_file_backend),
):
    user_id = current_user["id"]
    pcp_provider = None
    pcp_npi = current_user.get("pcp_npi")
    if pcp_npi:
        try:
            pcp_provider = await client.async_lookup(pcp_npi)
        except NPPESError:
            pass
    active_grant = storage.get_active_staff_access_grant(user_id)

    ehr_vendors = _ehr_vendor_ui_list(user_id, storage)
    ehr_enabled = bool(ehr_vendors)

    # Re-fetch only the signature fields so the page reflects edits made
    # earlier in the same request, but keep the rest of ``current_user``
    # intact — get_current_user computes ``is_org_admin`` (and similar
    # session-derived flags) that aren't on the users row, and merging
    # the storage row wholesale would drop them.
    sig_keys = (
        "credentials",
        "individual_npi",
        "state_license_number",
        "state_license_state",
        "signature_image_ref",
    )
    fresh_row = storage.get_user_by_id(user_id) or {}
    user_for_template: dict = {**current_user}
    for k in sig_keys:
        if k in fresh_row:
            user_for_template[k] = fresh_row.get(k)
    signature_image_url = await _signature_image_url(
        file_backend, user_for_template.get("signature_image_ref")
    )

    return render(
        "profile.html",
        {
            "request": request,
            "active_page": "profile",
            "saved_count": saved_count(storage, user_id),
            "user": user_for_template,
            "pcp_provider": pcp_provider,
            "mapbox_token": MAPBOX_TOKEN,
            "delete_error": None,
            "active_grant": active_grant,
            "ttl_options": TTL_OPTIONS,
            "ehr_enabled": ehr_enabled,
            "ehr_error": ehr_error,
            "ehr_vendors": ehr_vendors,
            "us_states": US_STATES,
            "signature_image_url": signature_image_url,
            "signature_saved": signature_saved,
            "signature_error": signature_error,
        },
    )


def _coerce_optional_form(value: str | None, *, max_length: int) -> str | None:
    """Strip + truncate-check a form input, returning None on empty input."""
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if len(cleaned) > max_length:
        raise HTTPException(
            status_code=422, detail=f"Field is too long (max {max_length} characters)."
        )
    return cleaned


@router.post("/profile/signature", response_class=HTMLResponse)
async def profile_save_signature(
    request: Request,
    credentials: Annotated[str | None, Form(max_length=_CREDENTIALS_MAX_LENGTH)] = None,
    individual_npi: Annotated[str | None, Form(max_length=10)] = None,
    state_license_number: Annotated[str | None, Form(max_length=_LICENSE_NUM_MAX_LENGTH)] = None,
    state_license_state: Annotated[str | None, Form(max_length=2)] = None,
    current_user: dict = Depends(require_user),
    storage: StorageBase = Depends(get_storage),
):
    """Replace the four signature TEXT fields. Empty inputs clear the field."""
    user_id = current_user["id"]

    creds = _coerce_optional_form(credentials, max_length=_CREDENTIALS_MAX_LENGTH)
    npi_clean = _coerce_optional_form(individual_npi, max_length=10)
    if npi_clean is not None and not _NPI_RE.match(npi_clean):
        raise HTTPException(status_code=422, detail="Individual NPI must be exactly 10 digits.")
    license_num = _coerce_optional_form(state_license_number, max_length=_LICENSE_NUM_MAX_LENGTH)
    if license_num is not None and not _LICENSE_RE.match(license_num):
        raise HTTPException(
            status_code=422,
            detail="State license number may only contain letters, numbers, and dashes.",
        )
    license_state = _coerce_optional_form(state_license_state, max_length=2)
    if license_state is not None:
        license_state = license_state.upper()
        if license_state not in _US_STATE_CODES:
            raise HTTPException(status_code=422, detail=f"Unknown state code {license_state!r}.")

    storage.update_user_signature(
        user_id,
        credentials=creds,
        individual_npi=npi_clean,
        state_license_number=license_num,
        state_license_state=license_state,
    )
    audit.record(
        storage,
        action="user.signature_updated",
        request=request,
        actor_user_id=user_id,
        scope_user_id=user_id,
        metadata={
            "fields_set": [
                k
                for k, v in {
                    "credentials": creds,
                    "individual_npi": npi_clean,
                    "state_license_number": license_num,
                    "state_license_state": license_state,
                }.items()
                if v is not None
            ],
        },
    )
    if request.headers.get("HX-Request"):
        resp = Response(status_code=200)
        resp.headers["HX-Redirect"] = "/profile?signature_saved=1"
        return resp
    return RedirectResponse("/profile?signature_saved=1", status_code=303)


@router.post("/profile/signature/image", response_class=HTMLResponse)
async def profile_upload_signature_image(
    request: Request,
    file: Annotated[UploadFile, File(...)],
    current_user: dict = Depends(require_user),
    storage: StorageBase = Depends(get_storage),
    file_backend: StorageFileBackend = Depends(get_file_backend),
):
    """Upload a PNG or JPEG signature image (≤200 KB)."""
    user_id = current_user["id"]

    raw_len = request.headers.get("content-length")
    if raw_len:
        try:
            if int(raw_len) > _SIGNATURE_MAX_BYTES * 2:  # multipart envelope overhead
                raise HTTPException(status_code=413, detail="Signature image too large.")
        except ValueError:
            pass

    data = await file.read(_SIGNATURE_MAX_BYTES + 1)
    if len(data) > _SIGNATURE_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Signature image must be ≤ {_SIGNATURE_MAX_BYTES // 1024} KB.",
        )
    if not data:
        raise HTTPException(status_code=422, detail="Signature image is empty.")

    try:
        mime = sniff_mime(data)
    except MimeSniffError as exc:
        raise HTTPException(status_code=415, detail=str(exc))
    if mime not in _SIGNATURE_ALLOWED_MIMES:
        raise HTTPException(
            status_code=415,
            detail="Signature image must be PNG or JPEG.",
        )

    suffix = _SIGNATURE_MIME_TO_SUFFIX[mime]
    object_path = f"user-{user_id}/signature/{uuid.uuid4().hex}.{suffix}"

    # Capture the prior ref (if any) so we can clean up the old object
    # after the new one lands successfully.
    fresh_user = storage.get_user_by_id(user_id) or {}
    prior_ref = fresh_user.get("signature_image_ref")

    try:
        await file_backend.put(path=object_path, data=data, mime_type=mime)
    except StorageFileError as exc:
        logger.exception("Signature image upload failed for user %s", user_id)
        raise HTTPException(status_code=502, detail=str(exc))

    storage.set_user_signature_image_ref(user_id, object_path)

    if prior_ref and prior_ref != object_path:
        try:
            await file_backend.delete(prior_ref)
        except Exception:
            logger.exception(
                "Failed to delete prior signature image %s for user %s", prior_ref, user_id
            )

    audit.record(
        storage,
        action="user.signature_image_updated",
        request=request,
        actor_user_id=user_id,
        scope_user_id=user_id,
        metadata={"mime_type": mime, "size_bytes": len(data)},
    )
    if request.headers.get("HX-Request"):
        resp = Response(status_code=200)
        resp.headers["HX-Redirect"] = "/profile?signature_saved=1"
        return resp
    return RedirectResponse("/profile?signature_saved=1", status_code=303)


@router.delete("/profile/signature/image", response_class=HTMLResponse)
async def profile_clear_signature_image(
    request: Request,
    current_user: dict = Depends(require_user),
    storage: StorageBase = Depends(get_storage),
    file_backend: StorageFileBackend = Depends(get_file_backend),
):
    """Clear the user's signature image (best-effort blob delete)."""
    user_id = current_user["id"]
    fresh_user = storage.get_user_by_id(user_id) or {}
    prior_ref = fresh_user.get("signature_image_ref")

    storage.set_user_signature_image_ref(user_id, None)

    if prior_ref:
        try:
            await file_backend.delete(prior_ref)
        except Exception:
            logger.exception(
                "Failed to delete signature image %s for user %s during clear", prior_ref, user_id
            )

    audit.record(
        storage,
        action="user.signature_image_cleared",
        request=request,
        actor_user_id=user_id,
        scope_user_id=user_id,
    )
    if request.headers.get("HX-Request"):
        resp = Response(status_code=200)
        resp.headers["HX-Redirect"] = "/profile?signature_saved=1"
        return resp
    return RedirectResponse("/profile?signature_saved=1", status_code=303)


@router.post("/profile/pcp/{npi}", response_class=HTMLResponse)
async def profile_set_pcp(
    request: Request,
    npi: str = Depends(require_valid_npi),
    current_user: dict = Depends(require_user),
    storage: StorageBase = Depends(get_storage),
):
    user_id = current_user["id"]
    storage.set_user_pcp(user_id, npi)
    resp = Response(status_code=200)
    resp.headers["HX-Redirect"] = "/profile"
    return resp


@router.delete("/profile/pcp", response_class=HTMLResponse)
async def profile_clear_pcp(
    request: Request,
    current_user: dict = Depends(require_user),
    storage: StorageBase = Depends(get_storage),
):
    storage.clear_user_pcp(current_user["id"])
    return render(
        "_pcp_section.html",
        {
            "request": request,
            "pcp_provider": None,
            "mapbox_token": MAPBOX_TOKEN,
        },
    )


@router.post("/profile/support-access", response_class=HTMLResponse)
async def profile_grant_support_access(
    request: Request,
    ttl_seconds: int = Form(default=DEFAULT_TTL_SECONDS),
    current_user: dict = Depends(require_user),
    storage: StorageBase = Depends(get_storage),
):
    """Create (or replace) a time-limited staff access grant for this user."""
    user_id = current_user["id"]
    valid_ttls = set(TTL_OPTIONS.values())
    if ttl_seconds not in valid_ttls:
        ttl_seconds = DEFAULT_TTL_SECONDS
    grant = storage.create_staff_access_grant(user_id=user_id, ttl_seconds=ttl_seconds)
    audit.record(
        storage,
        action="staff_access.granted",
        request=request,
        actor_user_id=user_id,
        scope_user_id=user_id,
        metadata={"grant_id": grant.id, "expires_at": grant.expires_at.isoformat()},
    )
    return render(
        "_support_access.html",
        {"request": request, "active_grant": grant, "ttl_options": TTL_OPTIONS},
    )


@router.delete("/profile/support-access", response_class=HTMLResponse)
async def profile_revoke_support_access(
    request: Request,
    current_user: dict = Depends(require_user),
    storage: StorageBase = Depends(get_storage),
):
    """Revoke the active staff access grant for this user."""
    user_id = current_user["id"]
    active_grant = storage.get_active_staff_access_grant(user_id)
    if active_grant:
        storage.revoke_staff_access_grant(user_id)
        audit.record(
            storage,
            action="staff_access.revoked",
            request=request,
            actor_user_id=user_id,
            scope_user_id=user_id,
            metadata={"grant_id": active_grant.id},
        )
    return render(
        "_support_access.html",
        {"request": request, "active_grant": None, "ttl_options": TTL_OPTIONS},
    )


@router.get("/profile/export-data.json")
async def profile_export_data(
    request: Request,
    current_user: dict = Depends(require_phi_consent),
    storage: StorageBase = Depends(get_storage),
):
    """Machine-readable export of all data associated with this user account."""
    user_id = current_user["id"]
    solo_scope = Scope(user_id=user_id, organization_id=None, membership_role=None)

    memberships = storage.list_memberships_for_user(user_id)
    active_memberships = [m for m in memberships if m.is_active]

    orgs_data = []
    for m in active_memberships:
        org = storage.get_organization(m.organization_id)
        orgs_data.append(
            {
                "organization_id": m.organization_id,
                "organization_name": org.name if org else None,
                "role": m.role,
                "joined_at": m.joined_at.isoformat(),
            }
        )

    providers = storage.list_providers(user_id)
    history = storage.get_history(limit=10000, user_id=user_id)

    # Solo-scope patients and referrals only (org data belongs to the org)
    patients = storage.list_patients(solo_scope, limit=10000)
    referrals = storage.list_referrals(solo_scope, limit=10000)

    # Audit log: all events where this user was the actor, plus all events on
    # their solo-scoped data (covers any admin or system access to their records).
    # Merge and deduplicate by id, sort newest-first.
    by_actor = storage.list_audit_events(actor_user_id=user_id, limit=10000)
    by_scope = storage.list_audit_events(scope_user_id=user_id, limit=10000)
    seen: set[int] = set()
    merged_events = []
    for ev in by_actor + by_scope:
        if ev.id not in seen:
            seen.add(ev.id)
            merged_events.append(ev)
    merged_events.sort(key=lambda e: e.created_at, reverse=True)

    def _ser(obj: object) -> str:
        if isinstance(obj, (datetime, date)):
            return obj.isoformat()
        raise TypeError(f"Not serializable: {type(obj)}")

    payload = {
        "exported_at": datetime.now(tz=timezone.utc).isoformat(),
        "profile": {
            "email": current_user.get("email"),
            "first_name": current_user.get("first_name"),
            "last_name": current_user.get("last_name"),
            "middle_name": current_user.get("middle_name"),
            "display_name": current_user.get("display_name"),
            "date_of_birth": current_user.get("date_of_birth"),
            "pcp_npi": current_user.get("pcp_npi"),
            "created_at": current_user.get("created_at"),
            "terms_accepted_at": current_user.get("terms_accepted_at"),
        },
        "organizations": orgs_data,
        "saved_providers": [p.model_dump() for p in providers],
        "search_history": [
            {"query_params": h.query_params, "searched_at": h.searched_at} for h in history
        ],
        "patients": [p.model_dump() for p in patients],
        "referrals": [r.model_dump() for r in referrals],
        "audit_log": [
            {
                "id": ev.id,
                "action": ev.action,
                "actor_user_id": ev.actor_user_id,
                "entity_type": ev.entity_type,
                "entity_id": ev.entity_id,
                "created_at": ev.created_at,
            }
            for ev in merged_events
        ],
    }

    # Serialize before auditing so a json.dumps failure doesn't log a phantom export.
    body = json.dumps(payload, default=_ser, indent=2)

    audit.record(storage, action="user.data_export", request=request, actor_user_id=user_id)

    export_date = datetime.now(tz=timezone.utc).date().isoformat()
    return Response(
        content=body,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="referme-data-export-{export_date}.json"',
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/profile/account/delete", response_class=HTMLResponse)
async def profile_delete_account(
    request: Request,
    confirm: str = Form(default="", max_length=50),
    current_user: dict = Depends(require_user),
    storage: StorageBase = Depends(get_storage),
    client: NPPESClient = Depends(get_client),
    file_backend: StorageFileBackend = Depends(get_file_backend),
):
    """Self-service account deletion with confirmation phrase."""
    user_id = current_user["id"]

    if confirm != _CONFIRM_PHRASE:
        pcp_provider = None
        pcp_npi = current_user.get("pcp_npi")
        if pcp_npi:
            try:
                pcp_provider = await client.async_lookup(pcp_npi)
            except NPPESError:
                pass
        return render(
            "profile.html",
            {
                "request": request,
                "active_page": "profile",
                "saved_count": saved_count(storage, user_id),
                "user": current_user,
                "pcp_provider": pcp_provider,
                "mapbox_token": MAPBOX_TOKEN,
                "delete_error": f'Type "{_CONFIRM_PHRASE}" exactly to confirm.',
            },
        )

    # Block sole org owners — they must transfer ownership first.
    memberships = storage.list_memberships_for_user(user_id)
    for m in memberships:
        if not m.is_active or not has_role_at_least(m.role, "owner"):
            continue
        org = storage.get_organization(m.organization_id)
        if org is None or org.deleted_at is not None:
            continue
        peers = storage.list_memberships_for_org(m.organization_id)
        other_owners = [
            p
            for p in peers
            if p.is_active and p.user_id != user_id and has_role_at_least(p.role, "owner")
        ]
        if not other_owners:
            org_name = org.name
            pcp_provider = None
            pcp_npi = current_user.get("pcp_npi")
            if pcp_npi:
                try:
                    pcp_provider = await client.async_lookup(pcp_npi)
                except NPPESError:
                    pass
            return render(
                "profile.html",
                {
                    "request": request,
                    "active_page": "profile",
                    "saved_count": saved_count(storage, user_id),
                    "user": current_user,
                    "pcp_provider": pcp_provider,
                    "mapbox_token": MAPBOX_TOKEN,
                    "delete_error": (
                        f'You are the sole owner of "{org_name}". '
                        "Transfer ownership or delete the organization before deleting your account."
                    ),
                },
            )

    # Audit BEFORE deletion so actor_user_id FK still resolves.
    # Omit entity_type/entity_id — the actor_user_id row gets SET NULL on delete,
    # which is the correct anonymization; storing entity_id as plain text would
    # preserve the user ID in the audit log after deletion.
    audit.record(
        storage,
        action="user.account_deleted",
        request=request,
        actor_user_id=user_id,
    )

    # Revoke the session row BEFORE deleting the user so the explicit revoke
    # succeeds (CASCADE would remove it anyway, but being explicit is safer and
    # prevents any race where a concurrent request re-creates the session).
    prior_session_id = request.session.get("session_id")
    request.session.clear()
    if prior_session_id:
        try:
            storage.revoke_session(prior_session_id)
        except Exception:
            pass

    storage_refs = storage.delete_user(user_id)

    # Best-effort blob cleanup — orphaned objects are recoverable via the
    # retention sweep, so we don't abort if this fails.
    for ref in storage_refs:
        try:
            await file_backend.delete(ref)
        except Exception:
            logger.exception("Failed to delete blob %s for deleted user %d", ref, user_id)

    if request.headers.get("HX-Request"):
        resp = Response(status_code=200)
        resp.headers["HX-Redirect"] = "/?deleted=1"
        return resp
    return RedirectResponse("/?deleted=1", status_code=303)
