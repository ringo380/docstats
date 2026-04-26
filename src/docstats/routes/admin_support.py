"""Admin support console — staff access to user accounts (grant-gated).

Staff can only view a user's account data when the user has created an active
staff_access_grant.  Every access is audited.  No PHI (patients/referrals) is
shown — only account fields and the user's own audit history.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse

from docstats.domain import audit
from docstats.routes._common import render
from docstats.routes.admin import _require_org, require_admin_scope
from docstats.scope import Scope
from docstats.storage import get_storage
from docstats.storage_base import StorageBase
from docstats.storage_base import normalize_email

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/support", tags=["admin"])


@router.get("", response_class=HTMLResponse)
async def admin_support(
    request: Request,
    email: str | None = Query(default=None, max_length=254),
    scope: Scope = Depends(require_admin_scope),
    storage: StorageBase = Depends(get_storage),
):
    """Search form — enter a user's email to look up their support access status."""
    org = _require_org(scope, storage)
    target_user = None
    active_grant = None
    not_found = False

    if email:
        normalized = normalize_email(email)
        target_user = storage.get_user_by_email(normalized)
        if target_user is None:
            not_found = True
        elif storage.get_membership(org.id, target_user["id"]) is None:
            # Treat out-of-org users as not found — avoid cross-tenant enumeration
            not_found = True
            target_user = None
        else:
            active_grant = storage.get_active_staff_access_grant(target_user["id"])
    return render(
        "admin/support.html",
        {
            "request": request,
            "active_section": "support",
            "org": org,
            "email_query": email or "",
            "target_user": target_user,
            "active_grant": active_grant,
            "not_found": not_found,
            "audit_events": [],
        },
    )


@router.get("/user/{user_id}", response_class=HTMLResponse)
async def admin_support_user(
    request: Request,
    user_id: int,
    scope: Scope = Depends(require_admin_scope),
    storage: StorageBase = Depends(get_storage),
):
    """View a user's account — requires an active staff access grant from the user."""
    org = _require_org(scope, storage)

    # Check grant before fetching any user data
    active_grant = storage.get_active_staff_access_grant(user_id)
    if not active_grant or not active_grant.is_active():
        audit.record(
            storage,
            action="staff_access.access_denied",
            request=request,
            actor_user_id=scope.user_id,
            scope_user_id=user_id,
            scope_organization_id=org.id,
        )
        return render(
            "admin/support.html",
            {
                "request": request,
                "active_section": "support",
                "org": org,
                "email_query": "",
                "target_user": None,
                "active_grant": None,
                "not_found": False,
                "audit_events": [],
                "access_denied": True,
            },
        )

    target_user = storage.get_user_by_id(user_id)
    if target_user is None or storage.get_membership(org.id, user_id) is None:
        return render(
            "admin/support.html",
            {
                "request": request,
                "active_section": "support",
                "org": org,
                "email_query": "",
                "target_user": None,
                "active_grant": None,
                "not_found": True,
                "audit_events": [],
                "access_denied": False,
            },
        )

    # Grant is active and user belongs to this org — load account-level audit events only.
    # We use actor_user_id (not scope_user_id) to avoid surfacing clinical data actions.
    audit_events = storage.list_audit_events(actor_user_id=user_id, limit=50)

    audit.record(
        storage,
        action="staff_access.accessed",
        request=request,
        actor_user_id=scope.user_id,
        scope_user_id=user_id,
        scope_organization_id=org.id,
        metadata={"grant_id": active_grant.id, "expires_at": active_grant.expires_at.isoformat()},
    )

    return render(
        "admin/support.html",
        {
            "request": request,
            "active_section": "support",
            "org": org,
            "email_query": "",
            "target_user": target_user,
            "active_grant": active_grant,
            "not_found": False,
            "audit_events": audit_events,
            "access_denied": False,
        },
    )
