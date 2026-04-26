"""Profile page and PCP management routes."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from docstats.auth import require_user
from docstats.client import NPPESClient, NPPESError
from docstats.domain import audit
from docstats.domain.orgs import has_role_at_least
from docstats.phi import require_phi_consent
from docstats.routes._common import MAPBOX_TOKEN, get_client, render, saved_count
from docstats.scope import Scope
from docstats.storage import get_storage
from docstats.storage_base import StorageBase
from docstats.storage_files import StorageFileBackend, get_file_backend
from docstats.validators import require_valid_npi

logger = logging.getLogger(__name__)

router = APIRouter(tags=["profile"])

_CONFIRM_PHRASE = "DELETE MY ACCOUNT"


@router.get("/profile", response_class=HTMLResponse)
async def profile(
    request: Request,
    current_user: dict = Depends(require_user),
    storage: StorageBase = Depends(get_storage),
    client: NPPESClient = Depends(get_client),
):
    user_id = current_user["id"]
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
            "delete_error": None,
        },
    )


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
            {"query_params": h.query_params, "searched_at": h.searched_at}
            for h in history
        ],
        "patients": [p.model_dump() for p in patients],
        "referrals": [r.model_dump() for r in referrals],
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
            p for p in peers
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
